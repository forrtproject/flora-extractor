"""run_open.py over the decision models not measured before 2026-11-03 (pinned dated ids).
Writes jev_eval_2026-10/out/<name>_full2.json; the 2026-11-03 results are copied into ./out/."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "jev_eval_2026-10"))  # run_open.py from PR #219
import run_open as R
R.MODELS.update({"mammoth1": "baseten/mammoth-1-20261021", "elephant1": "baseten/elephant-1-20261021",
                 "joey1": "baseten/joey-1-20261029", "d11": "liquid/d1.1-20261027",
                 "drex20": "nace-ai/drex-v2.0-20261009", "jev114": "typesafe/jev-1.14-20261014",
                 "msd1": "microsoft/microsoft-decision-1-20261009",
                 "span01": "respan/span-01-20260925", "span01lite": "respan/span-01-lite-20260925"})
R.run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
