"""Direct IR synthesis: narrations -> hypothesis -> seed IR -> extended IR."""
from .emit import emit
from .models import ProcessHypothesis, SynthesizedIR
from .oracle import replay, summary
from .run import SynthesisResult, select_seed, synthesize
from .trace import EvidenceTrace

__all__ = ["EvidenceTrace", "ProcessHypothesis", "SynthesisResult", "SynthesizedIR",
           "emit", "replay", "select_seed", "summary", "synthesize"]
