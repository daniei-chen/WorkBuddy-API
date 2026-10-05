"""Admin-only reconciliation of unknown upstream usage."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .. import budget, security

router=APIRouter(prefix='/api/budgets',tags=['budgets'])

class Resolution(BaseModel):
    tokens: int = Field(ge=0)
    credit: float = Field(ge=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=500)

@router.get('/unresolved')
def unresolved(user=Depends(security.require_admin)):
    return {'items':budget.unresolved()}

@router.post('/{request_id}/resolve')
def resolve(request_id:str,body:Resolution,user=Depends(security.require_admin)):
    try:
        return {'changed':budget.resolve(request_id,body.tokens,body.credit,user['username'],body.reason)}
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None
