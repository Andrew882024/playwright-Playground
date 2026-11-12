"""One shared attempt budget per integration run; failures consume slots."""
from dataclasses import dataclass


@dataclass
class RunBudget:
    gemini_remaining: int
    cover_remaining: int
    gemini_attempts: int = 0
    cover_attempts: int = 0

    def consume(self,stage):
        field=stage+'_remaining'
        if getattr(self,field)<=0:return False
        setattr(self,field,getattr(self,field)-1)
        setattr(self,stage+'_attempts',getattr(self,stage+'_attempts')+1)
        return True
