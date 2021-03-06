import logging
from flask.wrappers import Response
from werkzeug.exceptions import BadRequest, NotFound
from flask import Blueprint, redirect, send_file, request
from celestial.types import PDF

from aleph.core import archive, db
from aleph.model import Document, DocumentRecord, Audit
from aleph.logic.documents import update_document, delete_document
from aleph.logic.collections import update_collection
from aleph.logic.util import document_url
from aleph.logic.audit import record_audit
from aleph.views.cache import enable_cache
from aleph.views.util import get_db_document, get_index_document
from aleph.views.util import jsonify, parse_request, sanitize_html
from aleph.views.util import serialize_data
from aleph.serializers import RecordSchema
from aleph.serializers.entities import CombinedSchema, DocumentUpdateSchema
from aleph.search import DocumentsQuery, RecordsQuery

log = logging.getLogger(__name__)
blueprint = Blueprint('documents_api', __name__)


def _resp_canonical(resp, document_id):
    # EXPERIMENTAL HACK
    # the idea here is to tell search engines that they should not index
    # source documents, but instead go for the UI version of the site.
    link_header = '<%s>; rel="canonical"' % document_url(document_id)
    resp.headers['Link'] = link_header
    return resp


@blueprint.route('/api/2/documents', methods=['GET'])
def index():
    enable_cache()
    result = DocumentsQuery.handle(request, schema=CombinedSchema)
    return jsonify(result)


@blueprint.route('/api/2/documents/<int:document_id>')
def view(document_id):
    enable_cache()
    data = get_index_document(document_id)
    document = get_db_document(document_id)
    data['headers'] = document.headers
    # TODO: should this be it's own API? Probably so, but for that it would
    # be unclear if we should JSON wrap it, or serve plain with the correct
    # MIME type?
    if Document.SCHEMA_HTML in document.model.names:
        data['html'] = sanitize_html(document.body_raw, document.source_url)
    if Document.SCHEMA_TEXT in document.model.names:
        data['text'] = document.body_text
    if Document.SCHEMA_IMAGE in document.model.names:
        data['text'] = document.body_text
    record_audit(Audit.ACT_ENTITY, id=document_id)
    return serialize_data(data, CombinedSchema)


@blueprint.route('/api/2/documents/<int:document_id>', methods=['POST', 'PUT'])
def update(document_id):
    document = get_db_document(document_id, request.authz.WRITE)
    data = parse_request(DocumentUpdateSchema)
    document.update(data)
    db.session.commit()
    update_document(document)
    update_collection(document.collection)
    return view(document_id)


@blueprint.route('/api/2/documents/<int:document_id>', methods=['DELETE'])
def delete(document_id):
    document = get_db_document(document_id, request.authz.WRITE)
    delete_document(document)
    update_collection(document.collection)
    return ('', 204)


def _serve_archive(content_hash, file_name, mime_type):
    """Serve a file from the archive or by generating an external URL."""
    url = archive.generate_url(content_hash,
                               file_name=file_name,
                               mime_type=mime_type)
    if url is not None:
        return redirect(url)

    try:
        local_path = archive.load_file(content_hash, file_name=file_name)
        if local_path is None:
            return Response(status=404)

        return send_file(local_path,
                         as_attachment=True,
                         conditional=True,
                         attachment_filename=file_name,
                         mimetype=mime_type)
    finally:
        archive.cleanup_file(content_hash)


@blueprint.route('/api/2/documents/<int:document_id>/file')
def file(document_id):
    document = get_db_document(document_id)
    record_audit(Audit.ACT_ENTITY, id=document_id)
    resp = _serve_archive(document.content_hash,
                          document.safe_file_name,
                          document.mime_type)
    return _resp_canonical(resp, document_id)


@blueprint.route('/api/2/documents/<int:document_id>/pdf')
def pdf(document_id):
    document = get_db_document(document_id)
    record_audit(Audit.ACT_ENTITY, id=document_id)
    if not document.supports_pages:
        raise BadRequest("PDF is only available for text documents")
    file_name = document.safe_file_name
    if document.pdf_version != document.content_hash:
        file_name = '%s.pdf' % file_name
    resp = _serve_archive(document.pdf_version, file_name, PDF)
    return _resp_canonical(resp, document_id)


@blueprint.route('/api/2/documents/<int:document_id>/records')
def records(document_id):
    enable_cache()
    document = get_db_document(document_id)
    record_audit(Audit.ACT_ENTITY, id=document_id)
    if not document.supports_records:
        raise BadRequest("This document does not have records.")
    result = RecordsQuery.handle(request,
                                 document=document,
                                 schema=RecordSchema)
    return jsonify(result)


@blueprint.route('/api/2/documents/<int:document_id>/records/<int:index>')
def record(document_id, index):
    enable_cache()
    document = get_db_document(document_id)
    record_audit(Audit.ACT_ENTITY, id=document_id)
    if not document.supports_records:
        raise BadRequest("This document does not have records.")
    record = DocumentRecord.by_index(document.id, index)
    if record is None:
        raise NotFound("No such record: %s" % index)
    return serialize_data(record, RecordSchema)
