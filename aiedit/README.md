# AI Edit Plugin

Uses [sd-cli](https://github.com/leejet/stable-diffusion.cpp) (stable-diffusion.cpp) with a diffusion edit model and vision LLM to edit images based on text prompts.

## Usage

1. Open an image in GIMP
2. Go to Filters → AI → AI Edit...
3. Configure model paths (defaults provided) and enter your edit prompt
4. Click OK — the plugin saves the current layer, runs sd-cli, and loads the result as a new layer

## Dependencies

**stable-diffusion.cpp**

- See [sd-cli on GitHub](https://github.com/leejet/stable-diffusion.cpp) for updated install instructions.

## Get models

**Qwen Image 2.1 Turbo (default).**

The plugin defaults to [Qwen Image 2.1 Turbo](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo) with the Qwen3-VL 8B vision LLM. It edits the current image using the source image as a reference (`-r`) together with the vision LLM. It is a distilled model, so it only needs 8 steps at CFG scale 1.0; the plugin selects reference editing and the fixed Turbo sigma schedule automatically.

Download the four files with the [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli) (`pip install -U "huggingface_hub[cli]"`, or `curl -LsSf https://hf.co/cli/install.sh | bash`):

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

Matching defaults at the top of `aiedit.py` (set `MODELS_PATH` and `LLM_PATH` for your machine):

```python
DEFAULT_DIFFUSION_MODEL = MODELS_PATH + "/diffusion_models/qwen-image-2.1-turbo-Q4_K_M.gguf"
DEFAULT_LLM = LLM_PATH + "/Qwen3-VL-8B-Instruct-UD-IQ3_XXS.gguf"
DEFAULT_LLM_VISION = LLM_PATH + "/mmproj-F16.gguf"
DEFAULT_VAE = MODELS_PATH + "/vae/qwen_image_2.1_vae_bf16.safetensors"
```

**Boogu Edit (alternative).**

A Kontext-style model that has worked well for adding characters and text to images. Use Kontext mode and Guidance 0.0.

- gguf: https://huggingface.co/realrebelai/Boogu-Image-Edit_GGUFs/blob/main/boogu-edit-dit-Q4_0.gguf
- vae: https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/ae.safetensors

## Server

The same models can back the [SD Server](../sd-server/) plugin instead of calling `sd-cli` directly:

```bash
sd-server \
  --diffusion-model ~/Downloads/src/ComfyUI/models/diffusion_models/qwen-image-2.1-turbo-Q4_K_M.gguf \
  --vae ~/Downloads/src/ComfyUI/models/vae/qwen_image_2.1_vae_bf16.safetensors \
  --llm ~/models/Qwen3-VL-8B-Instruct-UD-IQ3_XXS/Qwen3-VL-8B-Instruct-UD-IQ3_XXS.gguf \
  --llm_vision ~/models/Qwen3-VL-8B-Instruct-UD-IQ3_XXS/mmproj-F16.gguf \
  --diffusion-fa --offload-to-cpu -v
```

## Model Paths

Default paths are configured in the plugin for the author's setup. Change them in the dialog or edit the defaults at the top of `aiedit.py`.

## Menu

`<Image>/Filters/AI/` — AI Edit...

## Thanks for trying out gimp-plugins!

* GitHub https://github.com/themanyone
* YouTube https://www.youtube.com/themanyone
* Mastodon https://mastodon.social/@themanyone
* Linkedin https://www.linkedin.com/in/henry-kroll-iii-93860426/
* Buy me a coffee https://buymeacoffee.com/isreality
* TheNerdShow.com https://thenerdshow.com
