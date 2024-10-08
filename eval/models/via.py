import copy
from vllm import LLM, SamplingParams
import gradio as gr
import librosa
import numpy as np
import torch
import torch.nn.functional as F
from datasets import Audio
from safetensors.torch import load, load_model
from torch import nn
import torch.nn.functional as F
from transformers import (
    AutoProcessor,
    AutoTokenizer,
    LlamaForCausalLM,
    WhisperForConditionalGeneration,
)
from torch.nn.parallel import DataParallel



class WhisperConnector(nn.Module):
    def __init__(
        self,
    ):
        super().__init__()
        self.decoder = None
        self.projection = nn.Linear(1280, 4096)
        self.query_tokens = nn.Parameter(torch.randn(448, 1280))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    def forward(self, x):
        bsz = x.shape[0]
        query_tokens = self.query_tokens[None, :, :].expand(bsz, -1, -1)
        virt_whisper_tokens = self.decoder(
            inputs_embeds=query_tokens, encoder_hidden_states=x
        )
        if self.projection.weight.shape[-1] == 5120:
            virtual_tokens = self.projection(virt_whisper_tokens[0].reshape(112, 5120))
        else:
            virtual_tokens = self.projection(virt_whisper_tokens[0])
        return virtual_tokens.to(self.device)


class VIA(nn.Module):
    def __init__(self, via_path):
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        whisper = (
            WhisperForConditionalGeneration.from_pretrained("openai/whisper-large-v3")
            .to(self.device)
            .eval()
        )
        connector = WhisperConnector()

        with open(via_path, "rb") as f:
            sd = load(f.read())

        with torch.no_grad():
            connector.query_tokens = nn.Parameter(sd["query_tokens"])
            connector.projection.weight = nn.Parameter(sd["projection.weight"].T)
            connector.projection.bias = nn.Parameter(sd["projection.bias"])
            connector.decoder = copy.deepcopy(whisper.model.decoder)
            wsd = {
                key.replace("connector.", ""): sd[key]
                for key in sd
                if key.startswith("connector.")
            }
            connector.decoder.load_state_dict(wsd)
        self.connector = connector.to(self.device)
        self.whisper_encoder = whisper.model.encoder.to(self.device)
        num_layers = 32
        num_gpus = 2
        device_map = dict(
            **{"model.embed_tokens": 1, "model.norm": 1, "lm_head": 2},
            **{
                "model.layers." + str(i): 1 + (i // (num_layers // num_gpus))
                for i in range(num_layers)
            },
        )
        # self.llm = LLM(model="meta-llama/Meta-Llama-3-8B-Instruct", 
        #     tensor_parallel_size=2,
        #     gpu_memory_utilization=0.5)

        self.llama_decoder = LlamaForCausalLM.from_pretrained(
            "meta-llama/Meta-Llama-3-8B-Instruct",
            device_map="auto",
            torch_dtype=torch.float16,
        ).eval()
        self.processor = AutoProcessor.from_pretrained("openai/whisper-large-v3")
        self.tokenizer = AutoTokenizer.from_pretrained("WillHeld/via-llama")
        self.prefix = torch.tensor([128000, 128006, 882, 128007, 271]).to(self.device)
        self.pre_system_suffix = torch.tensor(
            self.tokenizer.encode(
                "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            )
        ).to(self.device)
        self.pre_user_suffix = torch.tensor(
            self.tokenizer.encode(
                "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n"
            )
        ).to(self.device)
        
        self.final_header = torch.tensor([128009, 128006, 78191, 128007, 271]).to(
            self.device
        )

    def pad_or_trim(self, input_features, target_length=3000):
        input_device = input_features.device
        input_features = input_features.cpu()  # Move to CPU
        current_length = input_features.shape[-1]
        if current_length < target_length:
            padding = target_length - current_length
            padded_features = torch.zeros(*input_features.shape[:-1], target_length, dtype=input_features.dtype)
            padded_features[..., :current_length] = input_features
            return padded_features.to(input_device)  # Move back to original device
        elif current_length > target_length:
            return input_features[..., :target_length].to(input_device)
        return input_features.to(input_device)

    def prepare_batch_inputs(self, audio, prompts, system_prompts =None, padding=True):
        with torch.no_grad():
            inputs = self.processor(audio, return_tensors="pt", sampling_rate=16_000, padding=True)
            input_features = inputs.input_features.to(self.device)
            
            input_features = self.pad_or_trim(input_features, target_length=3000)
            input_features = input_features.to(self.device)
            hidden_states = self.whisper_encoder(input_features=input_features)["last_hidden_state"]
            
            virt_tokens = self.connector(hidden_states).squeeze().to(self.device)

            batch_size = virt_tokens.shape[0]

            if system_prompts:
                system_prompt_embeds = []
                for system_prompt in system_prompts:
                    system_prompt_ids = self.tokenizer.encode(system_prompt, add_special_tokens=False)
                    system_prompt_tensor = torch.tensor(system_prompt_ids, device=self.device)
            


            if prompts != None and prompts != "":
                prefix_embeds = []
                max_length = 0
                for i, prompt in enumerate(prompts):
                    if prompt:
                        user_prompt_tensor = torch.tensor(
                            self.tokenizer(prompt, add_special_tokens=False)["input_ids"],
                            device=self.pre_user_suffix.device,
                        )
                        if system_prompts:
                            prefix = torch.cat([self.pre_system_suffix, system_prompt_tensor[i], self.pre_user_suffix, user_prompt_tensor, self.prefix], axis=0)
                        else:
                            #With llama3 prompt formatting, we use a system suffix if there is only one prompt input, not user
                            prefix = torch.cat([self.pre_system_suffix, user_prompt_tensor, self.prefix], axis=0)
                    else:
                        prefix = self.prefix
                    embedded_prefix = self.llama_decoder.model.embed_tokens(prefix)
                    prefix_embeds.append(embedded_prefix)
                    max_length = max(max_length, embedded_prefix.shape[0])
                padded_embeds = [F.pad(embed, (0, 0, 0, max_length - embed.shape[0])) for embed in prefix_embeds]
                prefix_embeds = torch.stack(padded_embeds, dim = 0)
            else:
                prefix_embeds = self.llama_decoder.model.embed_tokens(self.prefix)
                prefix_embeds = prefix_embeds.unsqueeze(0).repeat(batch_size, 1, 1)
            suffix_embeds = self.llama_decoder.model.embed_tokens(self.final_header).unsqueeze(0).repeat(batch_size, 1, 1)
            input_embeds = torch.cat([prefix_embeds, virt_tokens, suffix_embeds], dim=1)

        return input_embeds

    def vllm_generate(
        self,
        audio_batch: np.ndarray,
        prompts: list[str],
        system_prompts: list[str],
        **kwargs

    ):
        input_embeds = self.prepare_batch_inputs(audio_batch, prompts, system_prompts, kwargs.get("padding", True))
        # Use VLLM for text generation
        sampling_params = SamplingParams(
            temperature=kwargs.get('temperature', 0.7),
            top_p=0.95,
            max_tokens=kwargs.get('max_new_tokens', 128),
        )

        outputs = self.llm.generate(input_embeds, sampling_params)

        # Process VLLM outputs
        decoded_outputs = [output.outputs[0].text for output in outputs]
        # Note: VLLM might not provide token-level log probs, so this part might need adjustment
        log_probs = [output.outputs[0].logprobs for output in outputs] if kwargs.get('return_log_probs', False) else None

        return None, decoded_outputs, log_probs

    def generate(
        self,
        audio_batch: np.ndarray,
        prompts: list[str],
        system_prompts: list[str],
        return_logprobs: bool = False,
        top_k_logprobs: int = 40,
        temperature: float = 0.001,
        do_sample: bool = False,
        max_new_tokens: int = 128,
        padding: bool = True,
    ):  
        with torch.no_grad():
            input_embeds = self.prepare_batch_inputs(audio_batch, prompts, system_prompts, padding)

            batch_size = input_embeds.shape[0]
            outs = [[] for _ in range(batch_size)]
            log_probs = [[] for _ in range(batch_size)]
            outputs = None
            not_finished = torch.ones(batch_size, dtype=torch.bool, device=self.device)

            while not_finished.any() and max(len(out) for out in outs) < max_new_tokens:
                past_key_values = outputs.past_key_values if outputs else None
                outputs = self.llama_decoder(
                    inputs_embeds=input_embeds.to(self.device).half(),
                    return_dict=True,
                    output_hidden_states=True,
                    past_key_values=past_key_values,
                )
                next_token_logits = outputs.logits[:, -1, :]

                probs = F.softmax(next_token_logits / temperature, dim=-1)

                if do_sample:
                    next_tokens = torch.multinomial(probs, num_samples=1).squeeze(1)
                else:
                    next_tokens = next_token_logits.argmax(dim=-1)

                if return_logprobs:
                    token_logprobs = torch.log(probs)
                    if top_k_logprobs:
                        token_logprobs, _ = torch.topk(token_logprobs, k=top_k_logprobs, dim=-1)

                for i in range(batch_size):
                    if not_finished[i]:
                        token = next_tokens[i].item()
                        outs[i].append(token)
                        if return_logprobs:
                            if top_k_logprobs:
                                logprobs[i].append(token_logprobs[i].tolist())
                            else:
                                logprobs[i].append(token_logprobs[i, token].item())
                        if next_tokens[i] == 128009:  # EOT token
                            not_finished[i] = False

                next_embeds = self.llama_decoder.model.embed_tokens(next_tokens.unsqueeze(1))
                input_embeds = next_embeds 
                del next_token_logits, probs, next_tokens

            decoded_outputs = []
            for out in outs:
                decoded = self.tokenizer.decode(out, skip_special_tokens=True).replace("<|eot_id|>", "")
                decoded_outputs.append(decoded)

        return outs, decoded_outputs, logprobs if return_logprobs else None

class ParallelVIA(nn.Module):
    def __init__(self, via_path):
        super().__init__()
        self.via = DataParallel(VIA(via_path))

    def generate(self, audio_batch, prompts, **kwargs):
        with torch.no_grad():
            outs, decoded_outputs, logprobs = self.via.module.generate(audio_batch, prompts, **kwargs)
            # Move results to CPU
            outs = [torch.tensor(out, device='cpu') for out in outs]
            if logprobs is not None:
                logprobs = [torch.tensor(prob, device='cpu') for prob in logprobs]
        torch.cuda.empty_cache()
        return outs, decoded_outputs, logprobs