
class AdmissionConfig(Strict):
    max_inflight: int = Field(default=1, ge=1, le=16)
    max_queue: int = Field(default=8, ge=0, le=256)
    timeout_seconds: int = Field(default=180, ge=1)
    result_ttl_seconds: int = Field(default=3600, ge=1)

