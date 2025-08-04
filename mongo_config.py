#!/usr/bin/env python3
"""
MongoDB configuration helper for production deployment
"""
import os
from pymongo import MongoClient
from urllib.parse import quote_plus

def get_mongo_client():
    """Get MongoDB client with proper SSL configuration for production"""
    mongo_uri = os.getenv("MONGO_URI")
    
    if not mongo_uri:
        raise ValueError("MONGO_URI environment variable not set")
    
    # Try multiple connection approaches
    connection_configs = [
        # Standard connection with SSL options
        {
            "uri": mongo_uri,
            "options": {
                "serverSelectionTimeoutMS": 5000,
                "connectTimeoutMS": 10000,
                "socketTimeoutMS": 10000,
                "tls": True,
                "tlsAllowInvalidCertificates": True,
                "retryWrites": True
            }
        },
        # Alternative with explicit SSL parameters
        {
            "uri": mongo_uri + "&ssl=true&ssl_cert_reqs=CERT_NONE",
            "options": {
                "serverSelectionTimeoutMS": 10000,
                "connectTimeoutMS": 20000,
                "socketTimeoutMS": 20000
            }
        },
        # Fallback basic connection
        {
            "uri": mongo_uri,
            "options": {}
        }
    ]
    
    for i, config in enumerate(connection_configs):
        try:
            print(f"Attempting MongoDB connection method {i+1}...")
            client = MongoClient(config["uri"], **config["options"])
            
            # Test the connection
            client.admin.command('ping')
            print(f"✅ MongoDB connection successful with method {i+1}")
            return client
            
        except Exception as e:
            print(f"❌ Connection method {i+1} failed: {e}")
            if i == len(connection_configs) - 1:
                raise Exception(f"All MongoDB connection methods failed. Last error: {e}")
            continue
    
    return None
