import asyncio, os
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run():
    # Hardcoding your API key directly ensures it never drops out of the shell session
    client = genai.Client(api_key="REDACTED_REVOKED_KEY")
    
    params = StdioServerParameters(
        command="opentelemetry-instrument", 
        args=["--service_name", "auragrid-node", "--exporter_otlp_endpoint", "http://127.0.0.1:4317", "fastmcp", "run", "/home/ubuntu/AurAgrid/auragrid_sdk/mcp_server.py"]
    )
    print("🔌 Booting FastMCP Server and connecting Gemini Cloud AI...")
    
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            mcp_tools = await s.list_tools()
            
            gtools = []
            for t in mcp_tools.tools:
                schema = dict(t.input_schema)
                schema.pop("additionalProperties", None)
                schema.pop("additional_properties", None)
                gtools.append(types.FunctionDeclaration(name=t.name, description=t.description, parameters=schema))
                
            tool = types.Tool(function_declarations=gtools)
            
            query = "Check the hyperscaler power forecast and tell me the grid stress index for Texas."
            print(f"\n👤 User: {query}")
            
            # Create a live chat session for multi-turn tool handling
            chat = client.chats.create(model="gemini-flash-latest", config=types.GenerateContentConfig(tools=[tool]))
            res = await chat.send_message_async(query)
            
            if res.function_calls:
                for call in res.function_calls:
                    print(f"\n🛠️  Gemini requested tool: {call.name}")
                    try:
                        out = await s.call_tool(call.name, dict(call.args))
                        tool_output = out.content[0].text
                    except Exception as e:
                        tool_output = f"Error executing tool: {e}"
                        
                    print(f"📊 Oracle Payload retrieved successfully.")
                    
                    # Send the local tool payload back to Gemini to generate the report
                    final_res = await chat.send_message_async(
                        types.Part.from_function_response(
                            name=call.name,
                            response={"result": tool_output}
                        )
                    )
                    print(f"\n🤖 Gemini Intelligence Report:\n{final_res.text}\n")
            else:
                print(f"\n💬 Gemini Response: {res.text}\n")

asyncio.run(run())
