import json
from pathlib import Path
from sandbox import WORKSPACE

MEM_PATH = WORKSPACE / "memory.json"

class MemoryStore:
    def __init__(self):
        if not MEM_PATH.exists():
            MEM_PATH.write_text(json.dumps({"user_preference": {}, "long_term": []}, ensure_ascii=False))
        self.data = json.loads(MEM_PATH.read_text(encoding="utf-8"))

    def save(self):
        MEM_PATH.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_memory(self, content: str):
        self.data["long_term"].append(content)
        self.save()

    def get_all(self):
        return self.data
