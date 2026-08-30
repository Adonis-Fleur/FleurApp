from flask import Flask, redirect
from .app_manager import discover_blueprints


def create_app():
    app = Flask(__name__)

    for bp in discover_blueprints():
        app.register_blueprint(bp)

    @app.route('/')
    def root():
        return redirect('/Home')

    return app
