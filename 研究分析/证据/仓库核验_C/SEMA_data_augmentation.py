import json
import random
from pathlib import Path

# === Configuration ===
INPUT_FILE = "../data/raw/matonto_train_pairs.json"
OUTPUT_DIR = Path("../data/train3")
COMBINED_FILE = "train.json"
FALSE_TO_TRUE_RATIO = 6
INCLUDE_INVERSE_NEGATIVES = False  # <<< Set to False to disable inverse negatives

PROMPT_TEMPLATE = 'Is "{child}" a subclass of "{parent}"? Answer with "true" or "false".'

# === Setup ===
OUTPUT_DIR.mkdir(exist_ok=True)
with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

true_pairs = {(d["parent"], d["child"]) for d in data}
types = list({item["parent"] for item in data} | {item["child"] for item in data})

true_examples = []
false_examples = []
false_pairs = set()

# === 1. Generate TRUE examples ===
for parent, child in true_pairs:
    example = {
        "user": PROMPT_TEMPLATE.format(parent=parent, child=child),
        "answer": "true"
    }
    true_examples.append(example)

# === 2.1 Inverse false examples (optional) ===
inverse_false_examples = []
if INCLUDE_INVERSE_NEGATIVES:
    for parent, child in true_pairs:
        inverse_pair = (child, parent)
        if inverse_pair not in true_pairs:
            example = {
                "user": PROMPT_TEMPLATE.format(parent=child, child=parent),
                "answer": "false"
            }
            inverse_false_examples.append(example)
            false_pairs.add(inverse_pair)

# Calculate total false examples needed
total_false_needed = len(true_examples) * FALSE_TO_TRUE_RATIO
remaining_false_needed = total_false_needed - len(inverse_false_examples)

# === 2.2 Random negative pairs ===
attempts = 0
max_attempts = remaining_false_needed * 20
while len(false_pairs) < total_false_needed and attempts < max_attempts:
    type1, type2 = random.sample(types, 2)
    pair = (type1, type2)
    if pair not in true_pairs and pair not in false_pairs:
        example = {
            "user": PROMPT_TEMPLATE.format(parent=type1, child=type2),
            "answer": "false"
        }
        false_examples.append(example)
        false_pairs.add(pair)
    attempts += 1

# Combine all false examples
if INCLUDE_INVERSE_NEGATIVES:
    false_examples.extend(inverse_false_examples)

# === 3. Create Combined File (1 true + ~5 falses) ===
combined = []
false_pool = false_examples.copy()
random.shuffle(false_pool)

false_per_true = FALSE_TO_TRUE_RATIO
false_index = 0

for true_example in true_examples:
    combined.append(true_example)
    selected_falses = false_pool[false_index:false_index + false_per_true]
    combined.extend(selected_falses)
    false_index += false_per_true

random.shuffle(combined)

# === 4. Wrap with system prompt and save ===
final_output = {
    "system": "You are an expert in ontology modeling, especially for material science. Decide if one concept is a subclass of another (i.e., more specific type or category). Answer with 'true' or 'false'. No explanations.",
    "prompts": combined
}

with open(OUTPUT_DIR / COMBINED_FILE, "w", encoding="utf-8") as f:
    json.dump(final_output, f, indent=2)

print(f"✅ Generated {len(true_examples)} true examples.")
print(f"✅ Generated {len(false_examples)} false examples.")
print(f"📁 Combined file saved to: {OUTPUT_DIR / COMBINED_FILE}")
