# SEMA-LLM4OL2025: Semantic Learning for Ontology Development

This repository contains the implementation for SEMA-LLM4OL2025, a project focused on training Large Language Models (LLMs) for ontology learning and semantic relationship classification tasks.

## 📋 Overview

The project implements a complete pipeline for:
- **Data augmentation** for ontology learning tasks
- **LoRA fine-tuning** of Llama 3.1 models
- **Inference** and evaluation on semantic relationship classification
- **Multi-GPU training** with FSDP support

## 🏗️ Repository Structure

```
SEMA-LLM4OL2025/
├── data/
│   ├── raw/           # Raw training data
│   ├── train/         # Processed training data
│   ├── val/           # Validation data
│   ├── test/          # Test data
│   └── predictions/   # Model predictions
├── src/
│   ├── config.json                # Training configuration
│   ├── train_lora.py             # LoRA training script
│   ├── inference.py              # Inference script
│   ├── data_augmentation.py      # Data augmentation utilities
│   ├── create_tokenized_datasets.py # Dataset tokenization
│   ├── filter_output.py          # Output filtering
│   ├── pipeline.sh               # Complete training pipeline
│   └── prepare_data/             # Data preprocessing utilities
├── requirements.txt
└── README.md
```

## 🚀 Quick Start

### 1. Environment Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set your Hugging Face token
export HF_TOKEN="your_huggingface_token"
```

### 2. Data Preparation

```bash
# Generate augmented training data
cd src
python data_augmentation.py

# Create tokenized datasets
python create_tokenized_datasets.py
```

### 3. Training

#### Single GPU Training
```bash
python train_lora.py --config_path config.json --hf_token $HF_TOKEN --use_fsdp false
```

#### Multi-GPU Training (FSDP)
```bash
fabric run \
  --accelerator=cuda \
  --devices=2 \
  --num-nodes=1 \
  train_lora.py \
  --config_path config.json \
  --hf_token $HF_TOKEN \
  --use_fsdp true
```

#### Complete Pipeline
```bash
# Run the complete training pipeline
bash pipeline.sh
```

### 4. Inference

```bash
python inference.py --config_path config.json --hf_token $HF_TOKEN
```

## ⚙️ Configuration

The main configuration is stored in `src/config.json`:

```json
{
    "model_name": "meta-llama/Llama-3.1-8B",
    "max_seq_length": 512,
    "micro_batch_size_train": 2,
    "micro_batch_size_eval": 2,
    "epochs": 30,
    "lr": 2e-5,
    "lora_train": {
        "lora_r": 8,
        "lora_alpha": 16,
        "lora_dropout": 0.05,
        "target_modules": [
            "self_attn.q_proj", "self_attn.k_proj", 
            "self_attn.v_proj", "self_attn.o_proj",
            "mlp.gate_proj", "mlp.up_proj", "mlp.down_proj"
        ]
    }
}
```

### Key Parameters:
- **model_name**: Base model to fine-tune (Llama 3.1-8B)
- **lora_r**: LoRA rank (controls adaptation capacity)
- **lora_alpha**: LoRA scaling parameter
- **target_modules**: Which model layers to apply LoRA to
- **lr**: Learning rate for training
- **epochs**: Maximum training epochs
- **lora_val_patience**: Early stopping patience

## 🔧 Features

### Data Augmentation
- **Balanced dataset creation** with configurable true/false ratios
- **Negative sampling** for robust classification
- **Template-based prompt generation**

### LoRA Training
- **Parameter-efficient fine-tuning** using LoRA adapters
- **Multi-GPU support** with FSDP (Fully Sharded Data Parallel)
- **Early stopping** with validation loss monitoring
- **Mixed precision training** (bf16) for efficiency

### Inference
- **Batch inference** on test datasets
- **Incremental prediction saving** to prevent data loss
- **Flexible output formatting**

## 🎯 Use Cases

This framework is designed for:
- **Ontology learning** and relationship classification
- **Semantic similarity** tasks
- **Subclass relationship** prediction
- **Knowledge graph** completion

## 📊 Training Monitoring

The training script provides:
- ✅ **Training loss** per epoch
- ❌ **Validation loss** tracking
- 💾 **Model checkpointing** at each epoch
- 🏆 **Best model selection** based on validation performance

## 🔬 Model Architecture

- **Base Model**: Llama 3.1-8B
- **Fine-tuning Method**: LoRA (Low-Rank Adaptation)
- **Precision**: Mixed precision (bf16)
- **Distributed Training**: FSDP for multi-GPU setups

## 📈 Performance

The model is optimized for:
- **Memory efficiency** through LoRA and gradient checkpointing
- **Training speed** with mixed precision and FSDP
- **Generalization** through early stopping and validation monitoring

## 🛠️ Advanced Usage

### Custom GPU Selection
```bash
# Use specific GPUs (e.g., GPU 0 and 2)
python train_lora.py --gpu_ids "0,2" --config_path config.json
```

### Data Filtering
```bash
# Filter and process predictions
python filter_output.py
```

### Custom Data Preparation
```bash
# Process your own data
python prepare_data/your_custom_preprocessor.py
```

## 📝 Citation

If you use this code in your research, please cite:

```bibtex
@misc{sema-llm4ol2025,
  title={SEMA-LLM4OL2025: Semantic Learning for Ontology Development},
  author={Your Name},
  year={2025},
  url={https://github.com/your-repo/SEMA-LLM4OL2025}
}
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙋‍♂️ Support

For questions or issues:
- Open an issue on GitHub
- Check the documentation in `src/`
- Review the configuration examples

---

**Note**: Make sure to set your Hugging Face token and have sufficient GPU memory for training Llama 3.1-8B models.