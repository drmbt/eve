from bson import ObjectId
from typing import Dict, Any, Optional, List
from mongo import MongoBaseModel, agents, tasks, models, users, threads


class Model(MongoBaseModel):
    name: str
    user: ObjectId
    slug: str = None
    args: Dict[str, Any]
    public: bool = False
    checkpoint: str
    thumbnail: str

    def __init__(self, **data):
        super().__init__(**data)
        self.make_slug()

    def make_slug(self):
        name = self.name.lower().replace(" ", "-")
        version = 1 + models.count_documents({"name": self.name, "user": self.user}) 
        username = users.find_one({"_id": self.user})["username"]
        self.slug = f"{username}/{name}/v{version}"

    def save(self):
        super().save(self, models)


class Task(MongoBaseModel):
    workflow: str
    args: Dict[str, Any]
    status: str = "pending"
    error: Optional[str] = None
    result: Optional[Any] = None
    user: ObjectId

    def __init__(self, **data):
        if isinstance(data.get('user'), str):
            data['user'] = ObjectId(data['user'])
        super().__init__(**data)

    def save(self):
        super().save(self, tasks)
    