"""Isolate every pytest invocation before production modules are imported."""
import os
import tempfile
os.environ['SONORA_DATA']=tempfile.mkdtemp(prefix='sonora-pytest-')
os.environ['SONORA_SESSION_TOKEN']='test-token-not-a-production-secret'

import copy
import importlib
import pytest

@pytest.fixture(autouse=True)
def isolate_legacy_harmony_state_models(request,monkeypatch):
    """Existing harmony tests use declared DSP-only model IDs, not real weights.

    Keep their state-transition scope while production selection now validates
    the model catalog. Catalog/inference validation has its own real and unit tests.
    """
    if request.module.__name__!='tests.test_harmony_review':return
    app_module=importlib.import_module('services.orchestrator.app')
    actual=app_module.pin_parameter_delta
    def pin(catalog,current,delta):
        if str(delta.get('model_id',current.get('model_id',''))).startswith('unit-'):return copy.deepcopy(delta)
        return actual(catalog,current,delta)
    monkeypatch.setattr(app_module,'pin_parameter_delta',pin)
