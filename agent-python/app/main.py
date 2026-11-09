from fastapi import FastAPI

app = FastAPI(title='Enterprise Agent Service')

@app.get('/health')
async def health():
    return {'status': 'ok'}
