"""The recordings, indexed so that a claim can be checked against them.

Nothing in this module knows what an invoice is. It knows only what the capture
layer emitted: events with an action, a sheet name, sometimes a column header,
sometimes a value. Sheets, columns, outcomes and keys are whatever the
recordings happen to contain, discovered by reading them.

That is deliberate. A hand-maintained vocabulary -- sheet X is entity Y, column
Z is field W -- makes a whole class of error impossible, but only for the one
application it was written for, and only until somebody adds a column. This
route asks the model to work the domain out from the evidence, so the evidence
is the only thing here that gets to say what exists.

What that leaves to check is narrower but sharper, and it is all
cross-examination rather than recall:

    you say Invoice.unitPrice is column "Unit Price" on sheet "Invoices",
    evidenced by THOROUGH_RELEASE:E003
    -> that event exists, and it read "Unit Price" on "Invoices". Agreed.

Events are addressed by a qualified reference, `SLUG:EVENT_ID`, e.g.
`PRICE_EXCESS:E003`. Bare event ids restart at E001 in every recording, so an
unqualified `E003` names seven different events once there is a corpus -- which
is precisely how a previous version of this pipeline came to cite real event
ids belonging to the wrong demonstration.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: The actions the capture layer emits. Not domain vocabulary -- these are
#: properties of the recorder, and a new one here means the recorder changed.
READ, WRITE, SEARCH, SELECT = "READ_CELL", "WRITE_CELL", "SEARCH", "SELECT_SHEET"


def slug_for(path: str | Path) -> str:
    """`evidence/recorded/price-excess.json` -> `PRICE_EXCESS`."""
    return Path(path).stem.upper().replace("-", "_")


@dataclass(frozen=True)
class Recording:
    slug: str
    source_file: str
    demonstration_id: str
    narration: str
    events: list[dict]

    @property
    def sheets(self) -> set[str]:
        return {e["sheet"] for e in self.events if e.get("sheet")}

    @property
    def outcome(self) -> str | None:
        """What this demonstration ended in: the last value written.

        Read off the trace rather than declared. Which column carries the
        verdict is not assumed -- in every recording the SME reads many cells
        and writes exactly one, and that write is the decision. If a recording
        ever writes twice, the last write is the one that stands.
        """
        for ev in reversed(self.events):
            if ev.get("action") == WRITE:
                return str(ev.get("value"))
        return None

    @property
    def subject(self) -> str | None:
        """What this demonstration is about: the first thing searched for."""
        for ev in self.events:
            if ev.get("action") == SEARCH:
                return str(ev.get("value"))
        return None

    def searched(self) -> dict[str, list[str]]:
        """sheet -> the values searched for on it.

        A search is how a row is located, so the value typed into one is the
        business key of whatever that sheet holds -- the only place a key
        appears, since nobody reads back the identifier they just searched for.
        """
        out: dict[str, list[str]] = {}
        for ev in self.events:
            if ev.get("action") == SEARCH and ev.get("sheet"):
                out.setdefault(ev["sheet"], []).append(str(ev.get("value")))
        return out


@dataclass
class EvidenceTrace:
    """A corpus of recordings, addressable by qualified event reference."""
    recordings: list[Recording] = field(default_factory=list)

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, paths) -> "EvidenceTrace":
        recs = []
        for p in sorted(Path(x) for x in paths):
            doc = json.loads(p.read_text())
            recs.append(Recording(
                slug=slug_for(p),
                source_file=str(p),
                demonstration_id=doc.get("demonstrationId", p.stem),
                narration=(doc.get("narration") or {}).get("text", ""),
                events=doc.get("events", []),
            ))
        return cls(recs)

    @classmethod
    def load_dir(cls, directory) -> "EvidenceTrace":
        return cls.load(sorted(Path(directory).glob("*.json")))

    def subset(self, slugs) -> "EvidenceTrace":
        wanted = set(slugs)
        return EvidenceTrace([r for r in self.recordings if r.slug in wanted])

    # -- lookup -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.recordings)

    @property
    def slugs(self) -> list[str]:
        return [r.slug for r in self.recordings]

    def _index(self) -> dict[str, dict]:
        idx: dict[str, dict] = {}
        for rec in self.recordings:
            for ev in rec.events:
                idx[f"{rec.slug}:{ev['eventId']}"] = ev
        return idx

    def event(self, ref: str) -> dict | None:
        return self._index().get(ref)

    def has(self, ref: str) -> bool:
        return ref in self._index()

    def unknown_refs(self, refs) -> list[str]:
        """The subset of `refs` that name no event in this corpus.

        Reported as a group rather than one at a time: a model that
        misunderstands the reference format gets every instance wrong, and
        fixing them one round trip each never converges.
        """
        idx = self._index()
        return [r for r in refs if r not in idx]

    # -- what the corpus shows, with no prior knowledge of the domain -----
    @property
    def sheets(self) -> set[str]:
        return {s for r in self.recordings for s in r.sheets}

    def columns(self) -> dict[str, set[str]]:
        """sheet -> every column header read or written on it."""
        out: dict[str, set[str]] = {s: set() for s in self.sheets}
        for rec in self.recordings:
            for ev in rec.events:
                if ev.get("column") and ev.get("action") in (READ, WRITE):
                    out.setdefault(ev["sheet"], set()).add(ev["column"])
        return out

    def written_columns(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for rec in self.recordings:
            for ev in rec.events:
                if ev.get("action") == WRITE and ev.get("column"):
                    out.setdefault(ev["sheet"], set()).add(ev["column"])
        return out

    def searched_sheets(self) -> set[str]:
        return {s for r in self.recordings for s in r.searched()}

    def observed_outcomes(self) -> dict[str, str]:
        return {r.slug: r.outcome for r in self.recordings if r.outcome}

    def values_read(self) -> dict[str, list[str]]:
        """Every value the corpus read, mapped to the events that read it.

        This is what lets a declared threshold be traced to the moment somebody
        looked it up, without anyone having to know which sheet holds thresholds.
        """
        out: dict[str, list[str]] = {}
        for rec in self.recordings:
            for ev in rec.events:
                if ev.get("action") != READ:
                    continue
                out.setdefault(str(ev.get("value")), []).append(f"{rec.slug}:{ev['eventId']}")
        return out

    def coverage(self) -> dict:
        return {
            "recordings": len(self.recordings),
            "sheets": sorted(self.sheets),
            "columns": {s: sorted(c) for s, c in sorted(self.columns().items())},
            "outcomes": sorted(set(self.observed_outcomes().values())),
            "searchedSheets": sorted(self.searched_sheets()),
        }

    def describe(self) -> str:
        """The corpus as the prompt shows it: what exists, not what it means."""
        lines = ["OBSERVED IN THE RECORDINGS (this is all that exists -- nothing else):"]
        columns = self.columns()
        written = self.written_columns()
        searched = self.searched_sheets()
        for sheet in sorted(self.sheets):
            marks = []
            if sheet in searched:
                marks.append("rows located by SEARCH")
            if sheet in written:
                marks.append(f"written: {', '.join(sorted(written[sheet]))}")
            suffix = f"   [{'; '.join(marks)}]" if marks else ""
            cols = ", ".join(f'"{c}"' for c in sorted(columns.get(sheet, ())))
            lines.append(f'  sheet "{sheet}"{suffix}\n      columns: {cols or "(none read)"}')
        lines.append(f"  values written as a verdict: "
                     f"{', '.join(sorted(set(self.observed_outcomes().values())))}")
        return "\n".join(lines)
