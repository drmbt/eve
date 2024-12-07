import os
import re
import yaml
import json
import random
import asyncio
import traceback
from abc import ABC, abstractmethod
from pydantic import BaseModel, create_model, ValidationError
from typing import Optional, List, Dict, Any, Type, Literal
from datetime import datetime, timezone
from instructor.function_calls import openai_schema

from sentry_sdk import add_breadcrumb, capture_exception, capture_message
import sentry_sdk
sentry_dsn = os.getenv("SENTRY_DSN")
sentry_sdk.init(dsn=sentry_dsn, traces_sample_rate=1.0, profiles_sample_rate=1.0)

from . import eden_utils
from .base import parse_schema
from .models import User
from .task import Task
from .mongo import Document, Collection, get_collection


@Collection("tools3")
class Tool(Document):
    """
    Base class for all tools.
    """

    key: str
    name: str
    description: str
    tip: Optional[str] = None
    
    output_type: Literal["boolean", "string", "integer", "float", "image", "video", "audio", "lora"]
    cost_estimate: str
    resolutions: Optional[List[str]] = None
    base_model: Literal["sd15", "sdxl", "sd3", "flux-dev", "flux-schnell"] = "sdxl"
    
    status: Optional[Literal["inactive", "stage", "prod"]] = "stage"
    visible: Optional[bool] = True
    allowlist: Optional[str] = None
    
    model: Type[BaseModel] #= None  # should this be optional?
    handler: Literal["local", "modal", "comfyui", "replicate", "gcp"] = "local"
    parent_tool: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    parameter_presets: Optional[Dict[str, Any]] = None
    gpu: Optional[str] = None    
    test_args: Dict[str, Any]


    @classmethod
    def _get_schema(cls, key: str, from_yaml: bool = False, db: str = "STAGE") -> dict:
        if from_yaml:
            api_files = get_api_files()
            if key not in api_files:
                raise ValueError(f"Tool {key} not found")            
            parent_api_file = api_files[key]
            with open(parent_api_file, 'r') as f:
                schema = yaml.safe_load(f)  
        else:
            schema = get_collection("tools3", db=db).find_one({"key": key})
        if schema.get("handler") == "comfyui":
            schema["workspace"] = parent_api_file.split('/')[-4]
        return schema
    
    @classmethod
    def get_sub_class(cls, schema: dict, from_yaml: bool = False, db: str = "STAGE") -> type:
        from .tools.local_tool import LocalTool
        from .tools.modal_tool import ModalTool
        from .tools.comfyui_tool import ComfyUITool
        from .tools.replicate_tool import ReplicateTool
        from .tools.gcp_tool import GCPTool

        parent_tool = schema.get('parent_tool')
        if parent_tool:
            parent_schema = cls._get_schema(parent_tool, from_yaml, db)
            handler = parent_schema.get("handler")
        else:
            handler = schema.get('handler')

        handler_map = {
            "local": LocalTool,
            "modal": ModalTool,
            "comfyui": ComfyUITool,
            "replicate": ReplicateTool,
            "gcp": GCPTool,
            None: LocalTool
        }
        
        tool_class = handler_map.get(handler, Tool)
        return tool_class

    @classmethod
    def convert_from_yaml(cls, schema: dict, file_path: str = None) -> dict:
        """
        Convert the schema into the format expected by the model.
        """

        parent_tool = schema.get("parent_tool")
        if parent_tool:
            parent_schema = cls._get_schema(parent_tool, from_yaml=True)
            parent_schema["parameter_presets"] = schema.pop("parameters", {})
            parent_parameters = parent_schema.pop("parameters", {})
            for k, v in parent_schema["parameter_presets"].items():
                parent_parameters[k].update(v)            
            parent_schema.update(schema)
            parent_schema['parameters'] = parent_parameters
            schema = parent_schema
        
        fields, model_config = parse_schema(schema)
        model = create_model(schema["key"], __config__=model_config, **fields)    
        model.__doc__ = eden_utils.concat_sentences(schema.get('description'), schema.get('tip', ''))
        schema["model"] = model

        if 'cost_estimate' in schema:
            schema['cost_estimate'] = str(schema['cost_estimate'])

        test_file = file_path.replace("api.yaml", "test.json")
        with open(test_file, 'r') as f:
            schema["test_args"] = json.load(f)

        return schema

    @classmethod
    def convert_from_mongo(cls, schema: dict) -> dict:
        schema["parameters"] = {
            p.pop("name"): {**(p.pop("schema")), **p} 
            for p in schema["parameters"]
        }
        fields, model_config = parse_schema(schema)
        model = create_model(schema["key"], __config__=model_config, **fields)    
        model.__doc__ = eden_utils.concat_sentences(schema.get('description'), schema.get('tip', ''))
        schema["model"] = model

        return schema

    @classmethod
    def convert_to_mongo(cls, schema: dict) -> dict:
        parameters = []
        for k, v in schema["parameters"].items():
            v['schema'] = {
                key: v.pop(key) 
                for key in ['type', 'items', 'anyOf']
                if key in v
            }
            parameters.append({"name": k, **v})

        schema["parameters"] = parameters
        schema.pop("model")
        
        return schema

    def save(self, db=None, **kwargs):
        super().save(db, {"key": self.key}, **kwargs)

    def _remove_hidden_fields(self, parameters):
        hidden_parameters = [k for k, v in parameters['properties'].items() if v.get('hide_from_agent')]
        for k in hidden_parameters:
            del parameters['properties'][k]
        parameters['required'] = [k for k in parameters['required'] if k not in hidden_parameters]

    def anthropic_schema(self, exclude_hidden: bool = False) -> dict[str, Any]:
        schema = openai_schema(self.model).anthropic_schema
        schema["input_schema"].pop("description")  # duplicated
        if exclude_hidden:
            self._remove_hidden_fields(schema["input_schema"])
        return schema

    def openai_schema(self, exclude_hidden: bool = False) -> dict[str, Any]:
        schema = openai_schema(self.model).openai_schema
        if exclude_hidden:
            self._remove_hidden_fields(schema["parameters"])
        return {
            "type": "function",
            "function": schema
        }

    def calculate_cost(self, args):
        if not self.cost_estimate:
            return 0
        cost_formula = self.cost_estimate
        cost_formula = re.sub(r'(\w+)\.length', r'len(\1)', cost_formula)  # Array length
        cost_formula = re.sub(r'(\w+)\s*\?\s*([^:]+)\s*:\s*([^,\s]+)', r'\2 if \1 else \3', cost_formula)  # Ternary operator
        
        cost_estimate = eval(cost_formula, args.copy())
        assert isinstance(cost_estimate, (int, float)), "Cost estimate not a number"
        return cost_estimate

    def prepare_args(self, args: dict):
        unrecognized_args = set(args.keys()) - set(self.model.model_fields.keys())
        if unrecognized_args:
            raise ValueError(f"Unrecognized arguments provided for {self.key}: {', '.join(unrecognized_args)}")

        prepared_args = {}
        for field, field_info in self.model.model_fields.items():
            if field in args:
                prepared_args[field] = args[field]
            elif field_info.default is not None:
                if field_info.json_schema_extra.get('randomize'):
                    minimum, maximum = field_info.metadata[0].ge, field_info.metadata[1].le
                    prepared_args[field] = random.randint(minimum, maximum)
                else:
                    prepared_args[field] = field_info.default
            else:
                prepared_args[field] = None
        
        try:
            self.model(**prepared_args)
        except ValidationError as e:
            print(traceback.format_exc())
            error_str = eden_utils.get_human_readable_error(e.errors())
            raise ValueError(error_str)

        return prepared_args

    def handle_run(run_function):
        """Wrapper for calling a tool directly and waiting for the result"""
        
        async def async_wrapper(self, args: Dict, db: str, mock: bool = False):
            try:
                args = self.prepare_args(args)
                add_breadcrumb(category="handle_run", data=args)
                if mock:
                    result = {"output": eden_utils.mock_image(args)}
                else:
                    result = await run_function(self, args, db)
                result["output"] = result["output"] if isinstance(result["output"], list) else [result["output"]]
                add_breadcrumb(category="handle_run", data=result)
                result = eden_utils.upload_result(result, db)
                add_breadcrumb(category="handle_run", data=result)
                result["status"] = "completed"
            except Exception as e:
                print(traceback.format_exc())
                result = {"status": "failed", "error": str(e)}
                capture_exception(e)
            return result
        
        return async_wrapper

    def handle_start_task(start_task_function):
        """Wrapper for starting a task process and returning a task"""

        async def async_wrapper(self, user_id: str, args: Dict, db: str, mock: bool = False):
            try:
                # validate args and user manna balance
                args = self.prepare_args(args)
                add_breadcrumb(category="handle_start_task", data=args)
                cost = self.calculate_cost(args)
                user = User.load(user_id, db=db)
                user.verify_manna_balance(cost)
                
            except Exception as e:
                print(traceback.format_exc())
                raise Exception(f"Task submission failed: {str(e)}. No manna deducted.")

            # create task and set to pending
            task = Task(
                tool=self.key, 
                parent_tool=self.parent_tool,
                output_type=self.output_type, 
                args=args, 
                user=user_id, 
                cost=cost,
                mock=mock
            )
            task.save(db=db)

            add_breadcrumb(category="handle_start_task", data=task.model_dump())

            # start task
            try:
                if mock:
                    handler_id = eden_utils.random_string()
                    output = {"output": eden_utils.mock_image(args)}
                    result = eden_utils.upload_result(output, db=db)
                    task.update(
                        handler_id=handler_id,
                        status="completed", 
                        result=result,
                        performance={"waitTime": (datetime.now(timezone.utc) - task.createdAt).total_seconds()}
                    )
                else:
                    handler_id = await start_task_function(self, task)
                    task.update(handler_id=handler_id)

                user.spend_manna(task.cost)            

            except Exception as e:
                print(traceback.format_exc())
                task.update(status="failed", error=str(e))
                capture_exception(e)
                raise Exception(f"Task failed: {e}. No manna deducted.")
            
            return task

        return async_wrapper

    def handle_wait(wait_function):
        """Wrapper for waiting for a task to complete"""

        async def async_wrapper(self, task: Task):
            if not task.handler_id:
                task.reload()
            try:
                if task.mock:
                    result = task.result
                else:
                    result = await wait_function(self, task)
            except Exception as e:
                print(traceback.format_exc())
                result = {"status": "failed", "error": str(e)}
            return result
        
        return async_wrapper

    def handle_cancel(cancel_function):
        """Wrapper for cancelling a task"""

        async def async_wrapper(self, task: Task):
            await cancel_function(self, task)
            n_samples = task.args.get("n_samples", 1)
            refund_amount = (task.cost or 0) * (n_samples - len(task.result or [])) / n_samples
            user = User.from_id(task.user, db=task.db)
            user.refund_manna(refund_amount)
            task.update(status="cancelled")
        
        return async_wrapper

    @abstractmethod
    async def async_run(self):
        pass

    @abstractmethod
    async def async_start_task(self):
        pass

    @abstractmethod
    async def async_wait(self):
        pass

    @abstractmethod
    async def async_cancel(self):
        pass

    def run(self, args: Dict, db: str, mock: bool = False):
        return asyncio.run(self.async_run(args, db, mock))

    def start_task(self, user_id: str, args: Dict, db: str, mock: bool = False):
        return asyncio.run(self.async_start_task(user_id, args, db, mock))
    
    def wait(self, task: Task):
        return asyncio.run(self.async_wait(task))
    
    def cancel(self, task: Task):
        return asyncio.run(self.async_cancel(task))


def get_tools_from_api_files(root_dir: str = None, tools: List[str] = None, include_inactive: bool = False) -> Dict[str, Tool]:
    """Get all tools inside a directory"""
    
    api_files = get_api_files(root_dir, include_inactive)
    
    all_tools = {
        key: Tool.from_yaml(api_file) 
        for key, api_file in api_files.items()
    }

    if tools:
        tools = {k: v for k, v in all_tools.items() if k in tools}
    else:
        tools = all_tools

    return tools

def get_tools_from_mongo(db: str, tools: List[str] = None, include_inactive: bool = False, prefer_local: bool = True) -> Dict[str, Tool]:
    """Get all tools from mongo"""
    
    filter = {"key": {"$in": tools}} if tools else {}
    tools = {}
    tools_collection = get_collection(Tool.collection_name, db=db)
    for tool in tools_collection.find(filter):
        try:
            tool["parameters"] = {p["name"]: {**(p.pop("schema")), **p} for p in tool["parameters"]}
            tool = Tool.from_schema(tool, db=db)
            if tool.status != "inactive" and not include_inactive:
                if tool.key in tools:
                    raise ValueError(f"Duplicate tool {tool.key} found.")
                tools[tool.key] = tool
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error loading tool {tool['key']}: {e}")

    return tools


def get_api_files(root_dir: str = None, include_inactive: bool = False) -> List[str]:
    """Get all tool directories inside a directory"""
    
    if root_dir:
        root_dirs = [root_dir]
    else:
        eve_root = os.path.dirname(os.path.abspath(__file__))
        root_dirs = [
            os.path.join(eve_root, tools_dir) 
            for tools_dir in ["tools", "../../workflows"]
        ]

    api_files = {}
    for root_dir in root_dirs:
        for root, _, files in os.walk(root_dir):
            if "api.yaml" in files and "test.json" in files:
                api_file = os.path.join(root, "api.yaml")
                with open(api_file, 'r') as f:
                    schema = yaml.safe_load(f)
                if schema.get("status") == "inactive" and not include_inactive:
                    continue
                key = schema.get("key", os.path.relpath(root).split("/")[-1])
                if key in api_files:
                    raise ValueError(f"Duplicate tool {key} found.")
                api_files[key] = os.path.join(os.path.relpath(root), "api.yaml")
            
    return api_files
