"""OnlyOffice integration schemas.

The editor config mirrors what ``DocsAPI.DocEditor`` expects (camelCase via
serialization aliases). The whole config is JWT-signed; the Document Server
verifies it with the shared secret from its local.json.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentType = Literal["word", "cell", "slide"]

EXTENSION_TYPES: dict[str, DocumentType] = {
    ".docx": "word",
    ".doc": "word",
    ".odt": "word",
    ".rtf": "word",
    ".xlsx": "cell",
    ".xls": "cell",
    ".ods": "cell",
    ".csv": "cell",
    ".pptx": "slide",
    ".ppt": "slide",
    ".odp": "slide",
}

# Formats OnlyOffice can save back losslessly; everything else opens read-only.
EDITABLE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".csv"}


class OfficeStatus(BaseModel):
    enabled: bool
    server_url: str | None  # base URL of the Document Server (for loading api.js)


class OfficeDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_type: str = Field(serialization_alias="fileType")
    key: str
    title: str
    url: str


class OfficeUser(BaseModel):
    id: str = "workbench-user"
    name: str = "Workbench"


UiTheme = Literal["dark", "light"]


def default_customization(theme: UiTheme = "dark") -> dict[str, Any]:
    """A calm, de-cluttered editor: dark chrome to match the app, compact header,
    no side panels or feedback nags. Unknown keys are ignored by older DS builds."""
    return {
        "autosave": True,
        "compactHeader": True,
        "compactToolbar": False,  # keep full editing power; header stays compact
        "hideRightMenu": True,
        "hideRulers": True,
        "toolbarHideFileName": True,
        "feedback": False,
        "help": False,
        "uiTheme": "theme-dark" if theme == "dark" else "theme-light",
        "features": {"spellcheck": {"change": True}},
    }


class OfficeEditorConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    callback_url: str = Field(serialization_alias="callbackUrl")
    mode: Literal["edit", "view"]
    lang: str = "en"
    user: OfficeUser = Field(default_factory=OfficeUser)
    customization: dict[str, Any] = Field(default_factory=default_customization)


class DocEditorConfig(BaseModel):
    """The object handed to ``new DocsAPI.DocEditor(elementId, config)``."""

    model_config = ConfigDict(populate_by_name=True)

    document: OfficeDocument
    document_type: DocumentType = Field(serialization_alias="documentType")
    editor_config: OfficeEditorConfig = Field(serialization_alias="editorConfig")
    token: str = ""  # JWT of this config (minus the token field itself)


class ForcesaveRequest(BaseModel):
    path: str


class LastSaveResponse(BaseModel):
    hash: str | None


class CallbackResponse(BaseModel):
    error: int = 0
