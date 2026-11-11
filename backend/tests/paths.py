"""Locations shared by tests without depending on their directory depth."""


from pathlib import Path
from touchline import ml_inference


ML_ROOT = Path(ml_inference.__file__).resolve().parent


ML_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ml_inference"
