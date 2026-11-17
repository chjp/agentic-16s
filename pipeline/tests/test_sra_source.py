import json
from pathlib import Path

import sra_source

FIX = Path(__file__).parent / "fixtures" / "ncbi_sdl_SRR578443.json"


def test_picks_normalized_not_lite_or_sff():
    loc = sra_source.pick_normalized(json.loads(FIX.read_text()))
    assert loc["name"] == "SRR578443"            # not SRR578443.lite (noqual) or the SFF (rehydration)
    assert loc["url"].startswith("https://sra-pub-run-odp.s3.amazonaws.com/")
    assert loc["md5"] == "56190dd1d468550f6f19436981e5bc88"


def test_none_when_only_lite():
    sdl = json.loads(FIX.read_text())
    for res in sdl["result"]:
        res["files"] = [f for f in res["files"] if f.get("noqual")]
    assert sra_source.pick_normalized(sdl) is None


def test_skips_paid_locations():
    sdl = {"result": [{"files": [{"type": "sra", "name": "X", "md5": "m",
                                  "locations": [{"service": "gs", "payRequired": True, "link": "https://g/x"}]}]}]}
    assert sra_source.pick_normalized(sdl) is None
