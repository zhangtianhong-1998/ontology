import argparse
import pandas as pd
import json
from sklearn.metrics import precision_score, recall_score, f1_score

# Set up command line argument parsing
parser = argparse.ArgumentParser(description='Evaluate generated data against true data.')
parser.add_argument('--gen_filename', type=str, required=True, help='CSV file containing generated data.')
parser.add_argument('--true_data_file', type=str, required=True, help='JSON file containing true data.')
parser.add_argument('--dataset', type=str, required=True, help='Dataset name (e.g., "foodon").')
args = parser.parse_args()

# Load the generated data from CSV
generated_df = pd.read_csv(args.gen_filename, delimiter=",", header=None, names=['head', 'relation', 'tail', 'Probability'])

# Remove leading and trailing whitespace from the generated DataFrame
generated_df['head'] = generated_df['head'].str.strip()
generated_df['relation'] = generated_df['relation'].str.strip()
generated_df['tail'] = generated_df['tail'].str.strip()

# Load the true data from JSON
with open(args.true_data_file, 'r') as f:
    true_data = json.load(f)

# Convert true data to a DataFrame
true_df = pd.DataFrame(true_data)

# Create sets of tuples for easier comparison
generated_set = set(zip(generated_df['head'], generated_df['relation'], generated_df['tail']))
true_set = set(zip(true_df['head'], true_df['relation'], true_df['tail']))

# Define the relations for which we want to add the reverse
reverse_relations = {"equivalentClass", "disjointWith"}

# Iterate through the DataFrame and add reverse relations to the set
for _, row in true_df.iterrows():
    if row['relation'] in reverse_relations:
        # Create the reverse relation tuple
        reverse_tuple = (row['tail'], row['relation'], row['head'])
        true_set.add(reverse_tuple)

# Create a unified set of all unique (head, relation, tail) tuples
all_tuples = generated_set.union(true_set)

# Create binary labels for precision and recall calculation
y_true = [1 if (head, relation, tail) in true_set else 0 for head, relation, tail in all_tuples]
y_pred = [1 if (head, relation, tail) in generated_set else 0 for head, relation, tail in all_tuples]

# Calculate precision, recall, and F1 score
precision = precision_score(y_true, y_pred, zero_division=0)
recall = recall_score(y_true, y_pred, zero_division=0)
f1 = f1_score(y_true, y_pred, zero_division=0)

# Print the results
print(f'\nPrecision: {precision:.4f}')
print(f'Recall: {recall:.4f}')
print(f'F1 Score: {f1:.4f}')
