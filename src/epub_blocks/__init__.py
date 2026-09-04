"""Public API for deterministic, recipe-driven EPUB text extraction."""

from importlib.metadata import version

from .errors import EpubBlocksError
from .extract import extract_blocks, extract_fragments
from .models import (
    CompiledBlock,
    CompiledRecipe,
    EpubPackage,
    ExtractedBlock,
    Fragment,
    NormalizationOptions,
    SpineDocument,
    TextBlock,
)
from .package import inspect_epub
from .recipe import (
    compile_recipe,
    compile_recipe_file,
    compiled_recipe_digest,
    extract_recipe,
    extract_recipe_file,
    load_recipe,
    write_tsv,
)
from .safety import SafetyLimits

__version__ = version("epub-blocks")

__all__ = [
    "CompiledBlock",
    "CompiledRecipe",
    "EpubBlocksError",
    "EpubPackage",
    "ExtractedBlock",
    "Fragment",
    "NormalizationOptions",
    "SafetyLimits",
    "SpineDocument",
    "TextBlock",
    "__version__",
    "compile_recipe",
    "compile_recipe_file",
    "compiled_recipe_digest",
    "extract_blocks",
    "extract_fragments",
    "extract_recipe",
    "extract_recipe_file",
    "inspect_epub",
    "load_recipe",
    "write_tsv",
]
