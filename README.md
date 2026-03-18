# CoupleApp 💑

A Flask web application designed for couples to manage shared activities, including favor tokens and collaborative shopping lists.

## Features

- **User Authentication**: Secure registration and login system
- **Token Management**: Exchange and track favor tokens between partners
- **Shopping Lists**: Create and share collaborative shopping lists for groceries and more
- **Real-time Progress**: Track completion status of tokens and shopping items
- **Admin Panel**: Protected admin interface for data management
- **Mobile Responsive**: Works seamlessly on all devices

## Tech Stack

- **Backend**: Flask (Python)
- **Database**: PostgreSQL (Production) / SQLite (Development)
- **Authentication**: Flask sessions with bcrypt
- **Deployment**: Railway

## Local Development

### Prerequisites

- Python 3.8+
- pip

### Installation

1. Clone the repository:
```bash
git clone <your-repo-url>
cd CoupleApp
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the application:
```bash
python app.py
```

The app will be available at `http://localhost:5000`

## Deployment on Railway

### Step 1: Prepare Your Repository

1. Create a new GitHub repository
2. Push your code to GitHub:
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

### Step 2: Deploy on Railway

1. Go to [Railway](https://railway.app)
2. Click "New Project"
3. Select "Deploy from GitHub repo"
4. Connect your GitHub account and select your repository
5. Railway will automatically detect the Python app and start deployment

### Step 3: Configure Database

The app is pre-configured with a PostgreSQL connection string. If you want to use your own database:

1. In Railway dashboard, go to your project
2. Click "New" → "Database" → "Add PostgreSQL"
3. Copy the `DATABASE_URL` from the PostgreSQL service
4. Update the connection string in `database.py` or set it as an environment variable

### Step 4: Environment Variables (Optional)

In Railway dashboard, you can set:
- `SECRET_KEY`: For production security (optional, has default)
- `DATABASE_URL`: If using a different database

## Usage

### Creating an Account
1. Navigate to the registration page
2. Choose a unique username (max 20 characters)
3. Set a password (min 6 characters)
4. Share your username with your partner

### Managing Tokens
1. **Navigate to Tokens**: Click "Tokens" in the navigation menu
2. **Create Token**: Click "Create Token" and select your partner
3. **Accept Token**: Click "Start" on received tokens
4. **Complete Token**: Click "Complete" when finished

### Shopping Lists
1. **Create List**: Go to Shopping → Create List
2. **Share List**: Select users during creation
3. **Add Items**: Use the form on list detail page
4. **Toggle Items**: Click checkbox to mark complete/incomplete

### Admin Panel
- Access at `/admin`
- Default password: `Tom123`
- Can clear all database data

## Database Schema

The app uses the following main tables:
- `users`: User accounts
- `tokens`: Favor tokens
- `shopping_lists`: Shopping list metadata
- `shopping_items`: Individual shopping items
- `shopping_list_members`: List sharing relationships

## Security Features

- Password hashing with bcrypt
- Session-based authentication
- SQL injection prevention
- Input sanitization
- CSRF protection

## Project Structure

```
CoupleApp/
├── app.py                 # Main application
├── database.py           # Database configuration
├── auth.py              # Authentication routes
├── token_routes.py      # Token management
├── shopping_routes.py   # Shopping lists
├── requirements.txt     # Dependencies
├── railway.toml        # Railway config
├── Procfile            # Deployment config
└── templates/          # HTML templates
    ├── tokens.html     # Token management page
    └── ...
```

## Contributing

Feel free to submit issues and enhancement requests!

## License

MIT License

## Support

For issues or questions, please create an issue in the GitHub repository.
