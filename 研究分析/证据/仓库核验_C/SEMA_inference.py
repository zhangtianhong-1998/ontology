import json
from pathlib import Path
from itertools import combinations
from typing import List
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel, PeftConfig
import numpy as np
import random
from jsonargparse import CLI

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def load_list(file_path: str) -> List[str]:
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def format_chat_prompt(type_1: str, type_2: str) -> str:
    bos = "<|begin_of_text|>"
    eot = "<|eot_id|>"
    start = "<|start_header_id|>"
    end = "<|end_header_id|>"

    return (
        f"{bos}{start}system{end}\n\n"
        "You are an expert in ontology modeling, especially for material science. "
        "Decide if one concept is a subclass of another (i.e., more specific type or category). "
        "Answer with 'true' or 'false'. No explanations."
        f"{eot}\n"
        f"{start}user{end}\n\n"
        f'Is "{type_1}" the parent class of "{type_2}"? Answer with "true" or "false". Answer:{eot}\n'
        f"{start}assistant{end}\n\n"
    )


def load_lora_model_and_tokenizer(peft_path: str, hf_token: str):
    config = PeftConfig.from_pretrained(peft_path)
    base_model = AutoModelForCausalLM.from_pretrained(
        config.base_model_name_or_path,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto",
        token=hf_token
    )
    model = PeftModel.from_pretrained(base_model, peft_path)
    model.eval()

    tokenizer = AutoTokenizer.from_pretrained(config.base_model_name_or_path, token=hf_token)
    tokenizer.pad_token = tokenizer.eos_token
    return tokenizer, model


@torch._dynamo.disable
def generate_relation(model, tokenizer, prompt: str, max_new_tokens: int, max_input_length: int):
    input_ids = tokenizer(prompt, return_tensors="pt", truncation=True, padding=True, max_length=max_input_length).input_ids.to(DEVICE)

    with torch.no_grad():
        output = model.generate(
            input_ids=input_ids,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id
        )

    decoded = tokenizer.decode(output[0], skip_special_tokens=True)
    return decoded


def main(config_path: Path = "config.json", hf_token: str = "you_token"):
    with open(config_path) as f:
        config = json.load(f)

    
    lr = config["lr"]
    epochs = config["epochs"]
    patience = config["lora_val_patience"]
    target_modules = config["lora_train"]["target_modules"]
    joined_target_modules = ['__'.join(target_modules[:i]) for i in range(1, len(target_modules)+1)]
    best_epoch_path = f"models/{config['model_name']}_{epochs}_max_ep_{patience}_pat_{lr}_lr_{joined_target_modules[-1]}_{seed}/best_epoch.txt"

    def get_best_epoch(txt_path: str) -> int:
        with open(txt_path, "r") as f:
            for line in f:
                if line.startswith("best_epoch="):
                    return int(line.strip().split("=")[1])
        raise ValueError("best_epoch not found in file.")

    best_epoch = get_best_epoch(best_epoch_path)

    peft_model_path = f"models/{config['model_name']}_{epochs}_max_ep_{patience}_pat_{lr}_lr_{joined_target_modules[-1]}_{seed}/epoch_{best_epoch}"

    types_file = config["types_file"]
    output_file = "../data/predictions/" + peft_model_path.replace("/", "_") + ".json"
    seed = config["seed"]
    max_new_tokens = config["max_new_tokens"]
    max_input_length = config["max_seq_length"]

    # Seed for reproducibility
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    types = load_list(types_file)

    try:
        with open(output_file, "r") as f:
            results = json.load(f)
    except FileNotFoundError:
        results = {}

    tokenizer, model = load_lora_model_and_tokenizer(peft_model_path, hf_token)

    for parent, child in combinations(types, 2):
        pair_id = f"{parent}__{child}"
        if pair_id not in results:
            prompt = format_chat_prompt(parent, child)
            print(f"\n🔍 Generating for: {parent} ⊆ {child}")
            generated_text = generate_relation(model, tokenizer, prompt, max_new_tokens, max_input_length)

            results[pair_id] = {
                "parent": parent,
                "child": child,
                "generated_text": generated_text,
                "prompt": prompt
            }

            with open(output_file, "w") as f:
                json.dump(results, f, ensure_ascii=False, indent=4)

    print(f"\n✅ Predictions saved to {output_file}")


if __name__ == "__main__":
    CLI(main)
