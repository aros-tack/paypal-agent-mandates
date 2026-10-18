import os

os.environ.setdefault("MANDATE_LLM_DISABLE", "1")        # unit tests never reach for a local model unless they pass a fake transport
