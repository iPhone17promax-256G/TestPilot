"""Optional helper; only run when the Agent decides it is useful."""
import json
import sys
data=json.loads(sys.argv[1]) if len(sys.argv)>1 else {}
print(json.dumps({"endpointCount":len(data.get("endpoints",[]))},ensure_ascii=False))
