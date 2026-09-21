"""Saved contacts: named recipient addresses for the Send picker.

A convenience list only. It never touches the node and never moves
coins; every send still goes through the normal review + confirmation
flow. Addresses are validated (Base58Check, S prefix) before storage.
Reads need a session + 2FA; changes also need CSRF and are audit-logged."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.batch_engine import validate_b3_address
from app.deps import AppState

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def _state(request: Request) -> AppState:
    return request.app.state.app_state


class ContactBody(BaseModel):
    label: str
    address: str


class RenameBody(BaseModel):
    label: str


def _clean_label(raw: str) -> str:
    label = " ".join((raw or "").split())[:64]
    if not label:
        raise HTTPException(status_code=422, detail="give the contact a name")
    return label


@router.get("")
async def list_contacts(request: Request):
    state = _state(request)
    state.require_2fa(request)
    return {"contacts": db.list_contacts(state.settings.db_path)}


@router.post("")
async def add_contact(body: ContactBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    label = _clean_label(body.label)
    address = (body.address or "").strip()
    if not validate_b3_address(address):
        raise HTTPException(status_code=422, detail="not a valid B3 address")
    try:
        cid = db.add_contact(state.settings.db_path, label, address)
    except ValueError as exc:
        if str(exc) == "duplicate":
            raise HTTPException(status_code=409,
                                detail="that address is already in your contacts")
        raise HTTPException(status_code=409, detail="contact list is full")
    db.audit(state.settings.db_path, "contact_add", sess.username,
             detail=f"address={address} label={label}")
    return {"ok": True, "id": cid}


@router.put("/{contact_id}")
async def rename_contact(contact_id: int, body: RenameBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    label = _clean_label(body.label)
    if not db.rename_contact(state.settings.db_path, contact_id, label):
        raise HTTPException(status_code=404, detail="contact not found")
    db.audit(state.settings.db_path, "contact_rename", sess.username,
             detail=f"id={contact_id} label={label}")
    return {"ok": True}


@router.delete("/{contact_id}")
async def delete_contact(contact_id: int, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    if not db.delete_contact(state.settings.db_path, contact_id):
        raise HTTPException(status_code=404, detail="contact not found")
    db.audit(state.settings.db_path, "contact_delete", sess.username,
             detail=f"id={contact_id}")
    return {"ok": True}
