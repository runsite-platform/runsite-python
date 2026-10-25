import sys
from pathlib import Path

import runsite

source_path = Path(sys.argv[1])
object_key = f"uploads/{source_path.name}"
bucket = runsite.storage.bucket()
bucket.upload(object_key, source_path)
print(bucket.presigned_url(object_key))
