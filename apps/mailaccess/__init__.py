from flask import Blueprint, Response, jsonify, render_template, request

from .backend import ensure_backend

bp = Blueprint(
    'mailaccess',
    __name__,
    url_prefix='/MailAccess',
    template_folder='templates',
)


@bp.route('/', strict_slashes=False)
def index():
    return render_template('mailaccess.html')


@bp.post('/api/investigate')
def api_investigate():
    body = request.get_json(force=True) or {}
    email = (body.get('email') or '').strip()
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    resp = ensure_backend().post('/api/investigate', json={'email': email})
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get('content-type', 'application/json'))


@bp.get('/api/investigations')
def api_investigations():
    page = request.args.get('page', 1)
    resp = ensure_backend().get(
        '/api/investigations',
        params={'page': page, 'page_size': 100},
    )
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get('content-type', 'application/json'))


@bp.get('/api/report/<inv_id>')
def api_report(inv_id):
    resp = ensure_backend().get(f'/api/report/{inv_id}')
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get('content-type', 'application/json'))


@bp.delete('/api/investigation/<inv_id>')
def api_delete(inv_id):
    resp = ensure_backend().delete(f'/api/investigation/{inv_id}')
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get('content-type', 'application/json'))