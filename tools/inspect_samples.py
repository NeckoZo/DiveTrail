"""Run real OCR on the supplied reference images, retaining positions for parser QA."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from matchlog.recognition import Recognizer

if __name__ == '__main__':
    out = ROOT / 'output' / 'ocr'
    out.mkdir(parents=True, exist_ok=True)
    engine = Recognizer()
    for name in sys.argv[1:]:
        result = engine.read(Path(name))
        (out / (Path(name).stem + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
