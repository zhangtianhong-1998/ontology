# ELLMO-at-llms4ol


# Overview

This project was done for the LLMs4OL challenge 2025: [Website](https://sites.google.com/view/llms4ol2025/home)

More information on the challenge including leaderboards and datasets can be found from the website linked.

The ELLMO team focused on tasks A and D, extracting terms and types, and determining relationships between types

## Task A

Our approach to Task A consisted of two methods:

1. LLM-Centric Procedure: Utilize solely the LLM to generate terms and types in separate iterations
2. Classification Procedure: Utilize the LLM to generate terms and types in one fell swoop, then relying on a fine-tuned classification model to determine the correct elements and eliminate the generated noise.

All code can be found in the Task A folder

#### Steps to replicate results for LLM-centric procedure:
1. `cd TaskA`
2. `python -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. `bash run_llm_centric_procedure.sh` or `./run_llm_centric_procedure.sh`

#### Steps to replicate results for classification procedure:
1. run any/all of the notebooks in `TaskA/models`
2. `cd TaskA`
3. `python -m venv .venv`
4. `source .venv/bin/activate`
5. `pip install -r requirements.txt`
6. `bash run_classification_procedure.sh` or `./run_classification_procedure.sh`

## Task D

This code uses shirty, an internal api, to run the code, you will need to replace these calls with calls to the model not using shirty.

Our approach to Task D consisted of two methods

1. Cluster and then run a remove edges function
2. Find the nearest neighbors and then run a remove edges function

All code can be found in the Task D folder, please note, as of now, all files names are written in the file and will need to be changed for new data.

1. To run the clustering step, run 

`cluster_intial.py`

then run 

`cluster_to_full_links.py`

2. To run the vector database step, see the vector_database folder under Task D

3. To run the remove edges step

`remove_edges.py`

4. To get the F1, precision, and recall scores run

`comparison_taskd.py`