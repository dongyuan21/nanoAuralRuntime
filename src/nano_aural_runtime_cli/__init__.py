"""Headless CLI dispatcher package. It does not import torch or model weights."""

from .main import controlfoley_alias, main

__all__ = ["controlfoley_alias", "main"]
