# Skrift

A Litestar website framework with Google OAuth authentication and WordPress-like template resolution.

## Features

- **Google OAuth Authentication**: Secure user authentication via Google
- **WordPress-like Templates**: Hierarchical template resolution for pages
- **Dynamic Controllers**: Load controllers from `app.yaml` configuration
- **SQLAlchemy Integration**: Async database support with SQLite/PostgreSQL
- **Session Management**: Client-side encrypted cookie sessions

## Installation

### From Git Repository

```bash
uv pip install git+https://github.com/yourusername/skrift.git
```

### Local Development

```bash
git clone https://github.com/yourusername/skrift.git
cd skrift
uv sync
```

## Configuration

1. Copy `.env.example` to `.env` and configure:

```env
SECRET_KEY=your-secret-key-here
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=http://localhost:8080/auth/google/callback
DATABASE_URL=sqlite+aiosqlite:///app.db
DEBUG=true
```

2. Configure controllers in `app.yaml`:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
```

## Running the Application

### Development Server

```bash
python -m skrift
```

Or using the main entry point:

```bash
python main.py
```

The application will start on `http://localhost:8080` with hot reload enabled.

### Production

For production deployments, use a production ASGI server:

```bash
uvicorn skrift.asgi:app --host 0.0.0.0 --port 8080 --workers 4
```

## Project Structure

```
skrift/
   skrift/           # Main package
      asgi.py        # Application factory
      config.py      # Settings management
      controllers/   # Route handlers
      db/           # Database models
      lib/          # Utilities (Template class, etc.)
   templates/         # Jinja2 templates
   static/           # Static assets
   app.yaml          # Controller configuration
   main.py           # Development entry point
```

## Template Resolution

Templates follow WordPress-like hierarchical resolution:

### Pages (`/{path}`)
1. `page-{full-path}.html` (e.g., `page-services-web.html`)
2. `page-{slug}.html` (e.g., `page-web.html`)
3. `page.html`

## License

MIT
