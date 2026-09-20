# Overview

The ELLMO team at the LLMs4OL challenge participated in the Flagship Task, the Reuse Task, and the Taxonomy Task.  When the corresponding paper is published, we will update to include here.

## LLMs4OL 2026 Challenge

The [LLMs4OL Challenge](https://sites.google.com/view/llms4ol2026/challenge-overview?authuser=0) explores the automated construction of structured knowledge representations from text through Large Language Models for Ontology Learning. Building on the established LLMs4OL paradigm, this edition of the challenge introduces three tracks:

- **[Flagship Task](https://sites.google.com/view/llms4ol2026/flagship-task?authuser=0):** End-to-end construction of a primitive ontology (terms, types, taxonomy, and relations) from raw text.
- **[Reuse Task](https://sites.google.com/view/llms4ol2026/reuse-task?authuser=0):** Extending partial ontologies with new concepts and relations extracted from text.
- **[Taxonomy Task](https://sites.google.com/view/llms4ol2026/taxonomy-task?authuser=0):** Cross-domain generalization by inducing hierarchical taxonomies for unseen domains given source-domain ontologies.

---

## Use of AI Statement

Code in this repo was generated with the help of AI coding tools, including gpt5.3-codex.  Use caution when using for critical applications.


## Flagship Task

### Method A: Hybrid Method

This repository contains the code assocaited with the Ellmo Team's Hybrid method for triple extraction. We use a two-phase approach:

1. Entity Extraction via SpaCy with LLM Post-Processing: In this phase, SpaCy is used to identify candiate entities. Then deterministic cleaning and addtional LLM-based cleaning are performed.

2. Triple construction via clustering and LLM-refinement: In this phase, the HDBSCAN clustering algorithm is used to group semantically similar entities. Then Gemma 4 defines the relation structures among entities within and outside of the clusters.


#### Repository structure
- `main1.py`
    - end-to-end pipeline: load -> extract -> det clean -> vLLM clean -> write output JSON. Note: Expects a json file path as an argument.
- Modules:
    - `process_task_a_data.py`: JSON loading and parsing helpers (`load_json`, `generate_content_list`, `generate_ent_list`)
    - `noun_chunking.py`: spaCy noun chunk expansion + unioning mentions/names/nouns
    - `clean_up_entities.py`: deterministic cleaning (`det_ent_cleaning`) and model-based post-filter (`vllm_second_cleaning`)
    - `context_aware_clustering.py`: phase 2 of the tool (`llm_refinement`, `formalize_relations`, `format_clusters`, `cluster`, `run_context_aware_clustering`)
- Unit Test:
    - `unittest.py`

#### Installation
```
conda create -n myenv python=3.12
conda install pip
pip install -r requirements.txt
```

#### Where outputs go

Full output can be found under `verbo_preds.json`

### Method B: LLM pipeline

The LLM Pipeline creates an ontology using LLM methods for different steps of creating an ontology.

1. Extracting Terms and Types:  `dspy_combined.py`

2. Term Typing, or finding instance-of relationships: `find_instance_of.py`

3. Taxonomy Discovery or finding is-a relationships: `find_is_a.py`

4. Non-Taxonomic Discovery: `infer_schemas.py` and `train_schemas.py`

#### Repository structure

- `main2.py`
    - End-to-end Task A LLM pipeline: load input JSON -> extract terms/types -> find `instance-of` relations -> find `is-a` relations -> infer non-taxonomic relations -> combine outputs -> write final JSON. Note: expects a JSON file path through the `--input` argument.

- Modules:
    - `dspy_combined.py`: Extracts candidate terms and types from the input data using the DSPy/LLM extraction workflow.
    - `find_instance_of.py`: Performs term typing and predicts `instance-of` relationships between extracted terms and types.
    - `find_is_a.py`: Performs taxonomic discovery and predicts `is-a` relationships between candidate types.
    - `infer_schemas.py`: Performs non-taxonomic relation inference using trained schema/model artifacts.
    - `combine_pipeline_outputs.py`: Combines the `instance-of`, `is-a`, and non-taxonomic relation outputs into the final primitive ontology triples JSON file.
    - `train_schemas.py`: Trains or constructs schema/model artifacts for non-taxonomic relation discovery. This is used during model preparation and is not required for ordinary inference if trained artifacts are already included.
    - `non_taxonomic_relations.json`: Defines the non-taxonomic relation labels considered by the non-taxonomic discovery stage.
    - `relation_evidence_schemas_final.json`: Trained schema artifact used by the non-taxonomic discovery stage.
    - `learned_relation_models_final.json`: Trained relation model artifact used by the non-taxonomic discovery stage.

- Unit Test:
    - `unittest.py`

#### Where outputs go

Full output can be found under `final_primitive_ontology_triples.json`

## Reuse Task

### Task B: LLM reuse pipeline

The Task B LLM pipeline extends a partial ontology. The input may already contain terms, types, initial primitive ontology triples, or extended primitive ontology triples. The pipeline predicts additional `instance-of`, `is-a`, and non-taxonomic relations and writes an extended primitive ontology triples output.

#### Repository structure

- `main3.py`
    - End-to-end Task B reuse pipeline: load input JSON -> merge seed terms/types -> extract additional terms/types -> find new `instance-of` relations -> find new `is-a` relations -> infer new non-taxonomic relations -> combine outputs -> write final extended JSON. Note: expects a JSON file path through the `--input` argument.

- Modules:
    - `dspy_combined_taskb.py`: Extracts or merges candidate terms and types for Task B, including seed terms/types from the input where available.
    - `find_instance_of_taskb.py`: Performs term typing and predicts new `instance-of` relationships while optionally filtering known initial outputs.
    - `find_is_a_taskb.py`: Performs taxonomic discovery and predicts new `is-a` relationships while optionally filtering known initial outputs.
    - `infer_schemas_pipeline_taskb.py`: Wrapper for Task B non-taxonomic discovery. Builds the entity list, optionally includes entities from initial triples, calls the schema inference script, and filters initial triples if configured.
    - `infer_schemas_taskb.py`: Performs non-taxonomic relation inference using trained schema/model artifacts.
    - `combine_pipeline_outputs_taskb.py`: Combines Task B `instance-of`, `is-a`, and non-taxonomic predictions into the final extended primitive ontology triples JSON file.
    - `non_taxonomic_relations_taskb.json`: Defines the non-taxonomic relation labels considered by the Task B non-taxonomic discovery stage.
    - `relation_evidence_schemas_final_taskb.json`: Trained schema artifact used by the Task B non-taxonomic discovery stage.
    - `learned_relation_models_final_taskb.json`: Trained relation model artifact used by the Task B non-taxonomic discovery stage.

- Unit Test:
    - `unittest.py`

#### Where outputs go

Full output can be found under `final_extended_primitive_ontology_triples.json`

## Taxonomy Task

The Taxonomy Task is an experiment toolkit for ontology relationship modeling. It covers data extraction, embedding feature generation, classical and neural training, holdout evaluation, LLM-assisted `is-a` resolution, and report/figure generation.

### Setup

- Install uv if it is not already installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Run the setup script: `bash setup.sh`
- Remember to activate your venv before running any code: `source .venv/bin/activate`
- Remote LLM client methods are intentionally disabled in `taskc_query_shirty.py` (`get_litellm_client`, `get_endpoint_client`, `get_shirty_client`). Implement only the method(s) you plan to use in your local/private environment before running workflows that depend on them.

Code was developed on RHEL 8.10

### Key folders

- `src/llm4ol_fy26_taskc/`: taskc_main Python package (`data`, `ml`, `finetune`, `llm`, `viz`, `text`)
- `slurm/`: cluster job launchers and submit helpers
- `latex/`: report build assets and figure/table scripts
- `data/`: processed datasets, features, and generated figures
- `ray_results/`, `tb_logs*/`, `results_taxonomy*/`: experiment outputs and run artifacts

### Main workflows (from repo root)

1. Build core datasets
   - `python -m taskc_get_llm4ol_data`
   - `python -m taskc_get_taxonomic_relationships`
   - `python -m taskc_get_embeddings`
2. Train/evaluate models
   - `python -m taskc_fit_non_parametric`
   - `python -m taskc_fit_xgboost_with_ray`
   - `python -m taskc_train_on_embeddings`
3. Run finetune/holdout pipelines
   - `python -m taskc_train_taxonomy_similarity`
   - `python -m taskc_run_best_ordered_3way_on_test_data`
4. Generate analysis artifacts
   - `python -m taskc_pca_distance_data`
   - `python -m taskc_tsne_distance_data`
   - `python -m taskc_umap_distance_data`

For cluster execution, use scripts in `slurm/`. For reproducible pipeline context, see `pipeline_encoder_only.md` and `pipeline_downselect.md`.

### OntoLearner-Compatible Learners

This repo includes OntoLearner-compatible learner wrappers in `taskc_taskc_learners.py`.

- Package: `ol`
- Factory: `create_pipeline_learners(...)`
- Learners:
  - `DirectPromptingLearner`
  - `EmbeddingDownselectLearner`
  - `ThreeWayCrossEncoderLearner`

These taskc_learners expose `load`, `fit`, `predict`, and `fit_predict` with an OntoLearner-style interface.

#### Data preparation behavior in `load`

`load()` is configured to check for required data artifacts and, if missing, launch the data-download/extraction/cleaning modules automatically.

- Default data lineage: `cleaned`
- Auto-prepare default: `auto_prepare_data=True`
- Modules run when artifacts are missing:
  - `python -m taskc_get_llm4ol_data --data-variant <variant>`
  - `python -m taskc_get_taxonomic_relationships --data-variant <variant>`
  - `python -m taskc_prepare_taxonomy_relationships_spaces_only --overwrite`

You can disable this behavior with `load(auto_prepare_data=False)`.

#### Minimal usage

```python
from ol import create_pipeline_learners

taskc_learners = create_pipeline_learners(repo_root=".")

# Uses cleaned data by default and auto-prepares missing data artifacts.
embed = taskc_learners["embedding_downselect"]
embed.load()

fit_result = embed.fit(
    train_data={
        "holdout_groups": ["Biology and Life Sciences"],
        "taskc_data_variant": "cleaned",
    },
    task="is-a",
)
# predict() automatically consumes latest fit outputs unless overridden.
pred_result = embed.predict(
    eval_data={
        "test_data_dir": "data/test_data",
        "device": "auto",
    },
    task="is-a",
)
```

#### Pipeline behavior

- `DirectPromptingLearner`
  - `fit`: compatibility context only (no model training)
  - `predict`: runs `taskc_eval_is_a_matrix_llm`

- `EmbeddingDownselectLearner`
  - `fit`: embeddings -> random forest -> 2-way cross-encoder
  - `predict`: test embeddings (if needed) -> RF scoring -> 2-way cross-encoder direction assignment
  - `predict` defaults to latest `fit` artifacts (RF model + cross-encoder model)

- `ThreeWayCrossEncoderLearner`
  - `fit`: trains 3-way cross-encoder (`taskc_train_taxonomy_similarity` with ordered 3-way setup)
  - `predict`: runs best 3-way checkpoint inference (`taskc_run_best_ordered_3way_on_test_data`)
  - `predict` defaults to latest `fit` results directory when `results_dir` is not provided


### Where outputs go

- Feature/data artifacts: typically under `data/processed/`
- Models/checkpoints: commonly under `data/models/` and run-specific result folders
- Figures: typically under `data/figures/`
- Ray/TensorBoard logs: `ray_results/` and `tb_logs*/`
- Taxonomy/holdout summaries: `results_taxonomy*/`
                                                                        


