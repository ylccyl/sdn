#!/usr/bin/env python3
"""
简单的Flask测试脚本，验证Flask是否能正常启动
"""

from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return '<h1>Flask测试成功！</h1><p>如果你能看到这个页面，说明Flask工作正常。</p>'

@app.route('/api/test')
def test():
    return jsonify({'status': 'ok', 'message': 'API工作正常'})

if __name__ == '__main__':
    print("启动Flask测试服务器...")
    print("访问 http://localhost:5000")
    print("或 http://localhost:5000/api/test")
    app.run(host='0.0.0.0', port=5000, debug=False)

