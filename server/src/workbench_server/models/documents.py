"""New-document API schemas (M5 item 16).

The tree can already create an empty *file*; this creates a valid *document*. The
distinction is the whole point: an empty ``.docx`` is not a zero-byte file but a
zip of required OOXML parts, and a zero-byte file with an Office extension is
worse than no feature — Word refuses it and the user's first act in the new
product is repairing a corrupt file.
"""

from typing import Literal

from pydantic import BaseModel

#: The document kinds the tree can create. The token *is* the file extension, so
#: the UI offers them by name (Word / Excel / …) while the wire carries the
#: extension. Four need real content (a template is copied); three are trivially
#: empty (``.py``/``.txt``/``.md`` — an empty file with the right extension).
DocumentKind = Literal["docx", "xlsx", "pptx", "ipynb", "py", "txt", "md"]


class CreateDocumentRequest(BaseModel):
    path: str  # workspace-relative, forward slashes; must end with `.{kind}`
    kind: DocumentKind


class CreateDocumentResponse(BaseModel):
    path: str
    hash: str  # sha256 of the bytes written — same shape as WriteResponse
