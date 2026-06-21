from agents.base import BaseAgent


class ScaffolderAgent(BaseAgent):
    NAME          = "scaffolder"
    READ_ONLY     = False
    CONTRACT_ONLY = False  # writes contract files — prompt enforces scope, not tool restriction
