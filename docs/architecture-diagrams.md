# GTI MCP Server Architecture Diagrams

This document contains Mermaid diagrams for the GTI MCP Server architecture.

## Component Overview Diagram

```mermaid
graph TB
    subgraph "MCP Clients"
        A1[Claude Desktop]
        A2[Cline]
        A3[Cursor]
        A4[Custom Frontend]
    end

    subgraph "Transport Layer"
        B1[stdio - Local]
        B2[SSE/HTTP - Remote]
    end

    subgraph "GTI MCP Server"
        C1[MCP Tools]
        C2[VT API Client]
    end

    D[VirusTotal/GTI API]

    A1 --> B1
    A2 --> B1
    A3 --> B1
    A4 --> B2

    B1 --> C1
    B2 --> C1

    C1 --> C2
    C2 --> D

    style C1 fill:#e1f5ff
    style C2 fill:#e1f5ff
```

## Local Deployment Flow Diagram

```mermaid
sequenceDiagram
    participant Client as MCP Client
    participant Server as GTI MCP Server
    participant Env as Environment
    participant VT as VirusTotal API

    Client->>Server: Launch via stdio
    Server->>Env: Read VT_APIKEY
    Env-->>Server: API Key
    Client->>Server: Call tool (e.g., get_file_report)
    Server->>VT: API Request with VT_APIKEY
    VT-->>Server: Response
    Server-->>Client: Tool Result
```

## Cloud Deployment Flow Diagram

```mermaid
sequenceDiagram
    participant Frontend as Frontend Client
    participant CloudRun as Cloud Run (SSE)
    participant Auth as Auth Middleware
    participant Server as GTI MCP Server
    participant VT as VirusTotal API

    Frontend->>CloudRun: Connect to /sse endpoint
    CloudRun->>Auth: Validate X-Mcp-Authorization header
    Auth-->>CloudRun: Authorized
    CloudRun-->>Frontend: SSE Connection Established

    Frontend->>CloudRun: Call tool with api_key parameter
    CloudRun->>Server: Execute tool
    Server->>VT: API Request with client-provided api_key
    VT-->>Server: Response
    Server-->>CloudRun: Tool Result
    CloudRun-->>Frontend: SSE Event with Result
```
