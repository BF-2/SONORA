"""Isolate every pytest invocation before production modules are imported."""
import os
import tempfile
os.environ['SONORA_DATA']=tempfile.mkdtemp(prefix='sonora-pytest-')
os.environ['SONORA_SESSION_TOKEN']='test-token-not-a-production-secret'
