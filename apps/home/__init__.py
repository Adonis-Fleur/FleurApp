from flask import Blueprint, render_template
from launcher.app_manager import get_apps_info

bp = Blueprint(
    'home',
    __name__,
    url_prefix='/Home',
    template_folder='templates',
    static_folder='static',
)


@bp.route('/', strict_slashes=False)
def index():
    apps = get_apps_info()
    return render_template('home.html', apps=apps)
