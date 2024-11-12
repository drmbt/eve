import os
import asyncio
import modal
from datetime import datetime

from models import Task, User
from tools import handlers

app = modal.App(
    name="handlers3",
    secrets=[
        # modal.Secret.from_name("admin-key"),
        # modal.Secret.from_name("clerk-credentials"), # ?
        
        modal.Secret.from_name("s3-credentials"),
        modal.Secret.from_name("mongo-credentials"),
        # modal.Secret.from_name("replicate"),
        # modal.Secret.from_name("openai"),
        # modal.Secret.from_name("anthropic"),
        # modal.Secret.from_name("elevenlabs"),
        # modal.Secret.from_name("newsapi"),
        # modal.Secret.from_name("runway"),
        # modal.Secret.from_name("sentry"),
    ],   
)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .env({"COMFYUI_PATH": "/root", "COMFYUI_MODEL_PATH": "/root/models"}) 
    .env({"TEST_ALL": os.getenv("TEST_ALL")})
    .apt_install("git", "git-lfs", "libgl1-mesa-glx", "libglib2.0-0", "libmagic1", "ffmpeg")
    .pip_install(
        "httpx", "tqdm", "websocket-client", "gitpython", "boto3", "omegaconf",
        "requests", "Pillow", "fastapi==0.103.1", "python-magic", "replicate", 
        "python-dotenv", "pyyaml", "instructor==1.2.6", "torch==2.3.1", "torchvision", "packaging",
        "torchaudio", "pydub", "moviepy", "accelerate", "pymongo", "google-cloud-aiplatform")
    .env({"WORKSPACE": workspace_name}) 
    .copy_local_file(f"../{root_workflows_folder}/workspaces/{workspace_name}/snapshot.json", "/root/workspace/snapshot.json")
    .copy_local_file(f"../{root_workflows_folder}/workspaces/{workspace_name}/downloads.json", "/root/workspace/downloads.json")
    .run_function(install_comfyui)
    .run_function(install_custom_nodes, gpu=modal.gpu.A100())
    .copy_local_dir(f"../{root_workflows_folder}/workspaces/{workspace_name}", "/root/workspace")
    .env({"WORKFLOWS": test_workflows})
)


async def _execute(tool_name: str, args: dict, user: str = None, env: str = "STAGE"):
    # handler = handlers[tool_name]
    print("GO!!!")
    print(tool_name, args, user, env)
    result = {"ok": "hello"}
    
    # result = await handler(args, user, env=env)    
    return result
        

@app.function(image=image, timeout=3600)
async def run(tool_name: str, args: dict, user: str = None):
    result = await _execute(tool_name, args, user)
    return result




@app.function(image=image, timeout=3600)
async def submit(task_id: str, env: str):
    print("the task")
    task = Task.load(task_id, env=env)
    print(task)
    
    start_time = datetime.utcnow()
    queue_time = (start_time - task.createdAt).total_seconds()
    
    task.update(
        status="running",
        performance={"waitTime": queue_time}
    )

    try:
        print(task.workflow, task.args, task.user, env)
        result = await _execute(
            task.workflow, task.args, task.user, env=env
        )
        task_update = {
            "status": "completed", 
            "result": result
        }
        return task_update

    except Exception as e:
        print("Task failed", e)
        task_update = {"status": "failed", "error": str(e)}
        user = User.load(task.user, env=env)
        user.refund_manna(task.cost or 0)

    finally:
        run_time = datetime.utcnow() - start_time
        task_update["performance"] = {
            "waitTime": queue_time,
            "runTime": run_time.total_seconds()
        }
        task.update(**task_update)

    
@app.local_entrypoint()
def main():
    async def run_example_remote():
        result = await run.remote.aio(
            tool_name="reel",
            args={
                "prompt": "billy and jamie are playing tennis at wimbledon",
            }
        )
        print(result)
    asyncio.run(run_example_remote())


if __name__ == "__main__":
    async def run_example_local():
        # output = await _execute(
        #     tool_name="reel",
        #     args={
        #         "prompt": "Jack and Abey are learning how to code ComfyUI at 204. Jack is from Madrid and plays jazz music",
        #         "narrator": True,
        #         "music": True,
        #         "min_duration": 10
        #     },
        #     user="651c78aea52c1e2cd7de4fff" #"65284b18f8bbb9bff13ebe65"
        # )
        output = await _execute(
            tool_name="news",
            args={
                "subject": "entertainment"
            },
            user="651c78aea52c1e2cd7de4fff" #"65284b18f8bbb9bff13ebe65"
        )
        print(output)
    asyncio.run(run_example_local())