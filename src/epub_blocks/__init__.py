"""Public API for deterministic, recipe-driven EPUB text extraction."""

from importlib.metadata import version

from .errors import EpubBlocksError
from .extract import extract_blocks
from .models import (
    EpubPackage,
    ExtractedBlock,
    NormalizationOptions,
    SpineDocument,
    TextBlock,
)
from .package import inspect_epub
from .recipe import extract_recipe, extract_recipe_file, load_recipe, write_tsv
from .safety import SafetyLimits

__version__ = version("epub-blocks")

__all__ = [
    "EpubBlocksError",
    "EpubPackage",
    "ExtractedBlock",
    "NormalizationOptions",
    "SafetyLimits",
    "SpineDocument",
    "TextBlock",
    "__version__",
    "extract_blocks",
    "extract_recipe",
    "extract_recipe_file",
    "inspect_epub",
    "load_recipe",
    "write_tsv",
]
