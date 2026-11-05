"""Fixed failure shared by the ARC interface stubs."""

class ContractPending(RuntimeError):
    code = "CONTRACT_PENDING"

    def __init__(self):
        super().__init__(self.code)
