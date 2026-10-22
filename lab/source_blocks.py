"""Experimental presentation only; original source content still validates quotes."""
import json

from apps.agent_runtime.response_verification import ModelResponseVerifier


class SourceBlockResponseVerifier(ModelResponseVerifier):
    @staticmethod
    def render_input(data, images):
        metadata = {**data, "sources": [
            {key: value for key, value in source.items() if key != "content"}
            for source in data["sources"]
        ]}
        return [
            {"type": "text", "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True)},
            *[{"type": "text", "text": "Untrusted source content " + json.dumps({
                "source_id": source["id"], "characters": len(source["content"]),
            }, ensure_ascii=False, sort_keys=True) + "\n" + source["content"]}
              for source in data["sources"]],
            *images,
        ]
