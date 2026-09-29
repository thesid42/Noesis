from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_latency import combine_timings


def test_timing_join_uses_response_id_and_keeps_unmatched_calls_out():
    call = {"agent_id": "camera-closeup1", "request_id": "round1", "response_id": "r1",
            "model": "test", "input_tokens": 12, "output_tokens": 5,
            "media_time_s": 1, "observed_after_s": 2, "latency_ms": 1000}
    gateway = {"event": "provider_success", "response_id": "r1", "elapsed_ms": 702,
               "upstream_ms": 700, "gateway_processing_ms": 2}
    results = combine_timings([call, {**call, "response_id": "unmatched"}],
                              [{**gateway, "response_id": "other", "elapsed_ms": 999}, gateway])
    assert len(results) == 1
    assert results[0]["nebius_round_trip_ms"] == 700
    assert results[0]["gateway_processing_ms"] == 2
    assert results[0]["local_request_overhead_ms"] == 298
    with pytest.raises(ValueError, match="correlation"):
        combine_timings([call], [{**gateway, "elapsed_ms": 2000}])
