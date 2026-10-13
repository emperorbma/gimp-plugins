# AI Image

A GIMP 3 plugin that generates new images using `sd-cli` with a diffusion model based on a text prompt.

## Menu

`<Image>/File/Create/AI Image...`

## Dependencies

- `sd-cli` tool installed and available in PATH
- A diffusion model GGUF file (default: `qwen-image-2.1-turbo-Q4_K_M.gguf`)
- A text encoder / vision LLM (GGUF)
- A VAE model file

## Usage

1. Choose **File → Create → AI Image...**
2. Configure model paths and image dimensions
3. Enter a text prompt describing the image you want
4. Press **Shift+Enter** or click **OK**

The generated image will open in a new GIMP window.

## Models

The plugin defaults to Qwen Image 2.1 Turbo with the Qwen3-VL 8B vision LLM — the same files as the [AI Edit](../aiedit/) plugin. It only needs 8 steps at CFG scale 1.0 with the Turbo sigma schedule.

Download them with the [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli):

```shell
# Diffusion model: Qwen Image 2.1 Turbo, Q4_K_M GGUF
hf download unsloth/Qwen-Image-2.1-Turbo-GGUF qwen-image-2.1-turbo-Q4_K_M.gguf \
  --local-dir ~/Downloads/src/ComfyUI/models/diffusion_models

# Vision LLM and its mmproj projector: Qwen3-VL 8B
hf download unsloth/Qwen3-VL-8B-Instruct-GGUF \
  Qwen3-VL-8B-Instruct-UD-IQ3_XXS.gguf mmproj-F16.gguf \
  --local-dir ~/models/Qwen3-VL-8B-Instruct-UD-IQ3_XXS

# VAE
hf download Comfy-Org/Qwen-Image-2.1 vae/qwen_image_2.1_vae_bf16.safetensors \
  --local-dir ~/Downloads/src/ComfyUI/models
```

Matching defaults at the top of `aiimage.py` (set `MODELS_PATH` and `LLM_PATH` for your machine):

```python
DEFAULT_DIFFUSION_MODEL = MODELS_PATH + "/diffusion_models/qwen-image-2.1-turbo-Q4_K_M.gguf"
DEFAULT_LLM = LLM_PATH + "/Qwen3-VL-8B-Instruct-UD-IQ3_XXS.gguf"
DEFAULT_LLM_VISION = LLM_PATH + "/mmproj-F16.gguf"
DEFAULT_VAE = MODELS_PATH + "/vae/qwen_image_2.1_vae_bf16.safetensors"
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| Diffusion Model | Path to the diffusion model GGUF file |
| LLM | Path to the text encoder / vision LLM GGUF file |
| LLM Vision | Path to the vision projector (mmproj) file |
| VAE | Path to the VAE model file |
| T5XXL | Path to the T5XXL encoder (optional) |
| T5 | Path to the T5 encoder (optional) |
| LoRA | Path to the LoRA model (optional) |
| Width | Image width in pixels (64–8192) |
| Height | Image height in pixels (64–8192) |
| Steps | Number of sampling steps |
| CFG Scale | Classifier-free guidance scale |
| Guidance | Distilled guidance (flow-matching models) |
| Sigmas | Custom sigma schedule (Qwen Image 2.1 Turbo) |
| Prompt | Text description of the image to generate |

## License

GPLv3
