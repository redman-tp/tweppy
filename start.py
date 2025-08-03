#!/usr/bin/env python3
"""
Production startup script for Koyeb deployment
"""
import os
from webapp import app

if __name__ == "__main__":
    # Get port from environment (Koyeb sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # Run the app
    app.run(host='0.0.0.0', port=port, debug=False)
