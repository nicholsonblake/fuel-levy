# Power BI MCP Server

Query your Power BI data through Claude.

## Setup (one-time, ~10 minutes)

### 1. Create an Azure AD App Registration

1. Go to [Azure Portal](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**
2. Name it something like `PowerBI-Claude-MCP`
3. Click **Register**
4. Copy the **Application (client) ID** and **Directory (tenant) ID**
5. Go to **Certificates & secrets** → **New client secret** → copy the secret value

### 2. Grant Power BI API permissions

1. In your app registration → **API permissions** → **Add a permission**
2. Select **Power BI Service** → **Application permissions**
3. Add these permissions:
   - `Dataset.Read.All`
   - `Workspace.Read.All`
   - `Report.Read.All`
   - `Dataset.ReadWrite.All` (only if you want refresh capability)
4. Click **Grant admin consent**

### 3. Add the service principal to your Power BI workspaces

1. In Power BI (app.powerbi.com) → open each workspace → **Settings** → **Access**
2. Add your app's **Client ID** as a **Member** (or Viewer for read-only)

### 4. Configure the MCP server

```bash
cd mcp-powerbi
cp .env.example .env
# Edit .env with your Azure credentials
```

### 5. Install dependencies

```bash
pip install -r requirements.txt
```

### 6. Add to Claude Desktop

Edit your Claude Desktop config (`~/.claude/claude_desktop_config.json` on Mac/Linux, or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "powerbi": {
      "command": "python",
      "args": ["/full/path/to/mcp-powerbi/server.py"]
    }
  }
}
```

Restart Claude Desktop.

## Usage

Just ask Claude natural questions like:

- "What workspaces do we have in Power BI?"
- "Show me our sales reports"
- "What are the top 10 customers by revenue?"
- "Compare this quarter's sales to last quarter"
- "What tables are available in the sales dataset?"

Claude will automatically use the right tools to navigate workspaces, find datasets, and query data with DAX.
