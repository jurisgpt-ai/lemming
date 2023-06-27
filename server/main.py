import json
from pathlib import Path
from typing import List, Dict, cast

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from nl2ltl.declare.base import Template

from helpers.common_helper.file_helper import read_str_from_upload_file
from helpers.common_helper.static_data_helper import app_description
from helpers.nl2plan_helper.utils import temporary_directory
from helpers.nl2plan_helper.ltl2plan_helper import (
    compile_instance,
    get_goal_formula,
)
from helpers.nl2plan_helper.manage_formulas import (
    get_formulas_from_matched_formulas,
)
from helpers.nl2plan_helper.nl2ltl_helper import NL2LTLRequest, prompt_builder
from helpers.plan_disambiguator_helper.build_flow_helper import (
    get_build_flow_output,
)
from helpers.plan_disambiguator_helper.selection_flow_helper import (
    get_selection_flow_output,
)
from helpers.planner_helper.planner_helper import (
    get_landmarks_by_landmark_category,
    get_plan_topq,
    get_planner_response_model_with_hash,
)
from helpers.planner_helper.planner_helper_data_types import (
    LandmarksResponseModel,
    LemmingTask,
    LTL2PDDLRequest,
    LTLFormula,
    Plan,
    PlanDisambiguatorInput,
    PlanDisambiguatorOutput,
    PlannerResponseModel,
    PlanningTask,
    ToolCompiler,
    Translation,
)
from nl2ltl import translate
from nl2ltl.engines.gpt.core import GPTEngine, Models
from pddl.parser.domain import DomainParser
from pddl.parser.problem import ProblemParser

app = FastAPI(
    title="Lemming",
    description=app_description,
    version="0.0.1",
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    openapi_url="/api/v1/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def handle_flow_output(
    flow_output: PlanDisambiguatorOutput,
) -> PlanDisambiguatorOutput:
    if flow_output is None:
        raise HTTPException(status_code=422, detail="Unprocessable Entity")
    return flow_output


def check_pddl_input(plan_disambiguator_input: PlanDisambiguatorInput) -> None:
    if not PlanDisambiguatorInput.check_domain_problem(
        plan_disambiguator_input
    ):
        raise HTTPException(
            status_code=400, detail="Bad Request: domain or problem is empty"
        )


@app.get("/")
def hello_lemming() -> str:
    return "Hello Lemming!"


@app.post("/file_upload")
async def file_upload(file: UploadFile = File(...)) -> str:
    file_contents = read_str_from_upload_file(file)
    return file_contents


@app.post("/import_domain/{domain_name}")
def import_domain(domain_name: str) -> LemmingTask:
    planning_task = PlanningTask(
        domain=open(f"./data/{domain_name}/domain.pddl").read(),
        problem=open(f"./data/{domain_name}/problem.pddl").read(),
    )

    try:
        plans = json.load(open(f"./data/{domain_name}/plans.json"))
        plans = [Plan.parse_obj(item) for item in plans]

    except Exception as e:
        print(e)
        plans = []

    try:
        prompt = json.load(open(f"./data/{domain_name}/prompt.json"))
        nl_prompts = [Translation.parse_obj(item) for item in prompt]

    except Exception as e:
        print(e)
        nl_prompts = []

    new_lemming_task = LemmingTask(
        planning_task=planning_task, plans=plans, nl_prompts=nl_prompts
    )
    return new_lemming_task


@app.post("/get_landmarks/{landmark_category}")
async def get_landmarks(
    landmark_category: str,
    planning_task: PlanningTask,
) -> LandmarksResponseModel:
    if planning_task.domain is None or planning_task.problem is None:
        raise HTTPException(status_code=400, detail="Bad Request")

    landmarks = get_landmarks_by_landmark_category(
        planning_task, landmark_category
    )

    if landmarks is None:
        raise HTTPException(status_code=422, detail="Unprocessable Entity")

    return LandmarksResponseModel(landmarks=landmarks)


@app.post("/get_plans")
async def get_plans(planning_task: PlanningTask) -> PlannerResponseModel:
    if (
        planning_task.domain is None
        or planning_task.problem is None
        or len(planning_task.domain) == 0
        or len(planning_task.problem) == 0
    ):
        raise HTTPException(
            status_code=400, detail="Bad Request: domain or problem is empty"
        )

    planning_result = get_plan_topq(planning_task)

    if planning_result is None:
        raise HTTPException(status_code=422, detail="Unprocessable Entity")

    return get_planner_response_model_with_hash(planning_result)


@app.post("/generate_select_view")
def generate_select_view(
    plan_disambiguator_input: PlanDisambiguatorInput,
) -> PlanDisambiguatorOutput:
    check_pddl_input(plan_disambiguator_input)

    flow_output = get_selection_flow_output(
        plan_disambiguator_input.selection_infos,
        plan_disambiguator_input.landmarks,
        plan_disambiguator_input.domain,
        plan_disambiguator_input.problem,
        plan_disambiguator_input.plans,
    )

    return handle_flow_output(flow_output)


@app.post("/generate_build_forward")
def generate_build_forward(
    plan_disambiguator_input: PlanDisambiguatorInput,
) -> PlanDisambiguatorOutput:
    check_pddl_input(plan_disambiguator_input)

    flow_output = get_build_flow_output(
        plan_disambiguator_input.selection_infos,
        plan_disambiguator_input.landmarks,
        plan_disambiguator_input.domain,
        plan_disambiguator_input.problem,
        plan_disambiguator_input.plans,
        True,
    )

    return handle_flow_output(flow_output)


@app.post("/generate_build_backward")
def generate_build_backward(
    plan_disambiguator_input: PlanDisambiguatorInput,
) -> PlanDisambiguatorOutput:
    check_pddl_input(plan_disambiguator_input)

    flow_output = get_build_flow_output(
        plan_disambiguator_input.selection_infos,
        plan_disambiguator_input.landmarks,
        plan_disambiguator_input.domain,
        plan_disambiguator_input.problem,
        plan_disambiguator_input.plans,
        False,
    )

    return handle_flow_output(flow_output)


@app.post("/generate_nl2ltl_integration")
def generate_nl2ltl_integration(
    plan_disambiguator_input: PlanDisambiguatorInput,
) -> PlanDisambiguatorOutput:
    return generate_select_view(plan_disambiguator_input)


@app.post("/nl2ltl", response_model=None)
def nl2ltl(request: NL2LTLRequest) -> List[LTLFormula]:
    # TODO: Remove this after the backend is ready
    # Uncomment this to test the UI
    # ltl_formulas: List[LTLFormula] = [
    #     LTLFormula(
    #         user_prompt=request.utterance,
    #         formula="RespondedExistence Slack Gmail",
    #         description="If Slack happens at least once then Gmail has to happen or happened before Slack.",
    #         confidence=0.4,
    #     ),
    #     LTLFormula(
    #         user_prompt=request.utterance,
    #         formula="Response Slack Gmail",
    #         description="Whenever activity Slack happens, activity Gmail has to happen eventually afterward.",
    #         confidence=0.3,
    #     ),
    #     LTLFormula(
    #         user_prompt=request.utterance,
    #         formula="ExistenceTwo Slack",
    #         description="Slack will happen at least twice.",
    #         confidence=0.2,
    #     ),
    # ]

    # TODO: we currently use only the Toy Domain
    custom_prompt = prompt_builder(
        prompt_path=Path(r"data/Toy Domain/prompt.json").resolve()
    )

    with temporary_directory() as tmp_dir:
        tmp_file = Path(tmp_dir) / "tmp.json"
        tmp_file = tmp_file.resolve()
        tmp_file.write_text(custom_prompt, encoding="utf-8")

        engine = GPTEngine(model=Models.DAVINCI3.value, prompt=tmp_file)

    utterance = request.utterance
    matched_formulas: Dict[Template, float] = cast(
        Dict[Template, float], translate(utterance, engine)
    )
    ltl_formulas: List[LTLFormula] = get_formulas_from_matched_formulas(
        utterance, matched_formulas
    )
    return ltl_formulas


@app.post("/ltl_compile/{tool}")
def ltl_compile(request: LTL2PDDLRequest, tool: ToolCompiler) -> LemmingTask:
    domain_parser = DomainParser()
    problem_parser = ProblemParser()

    domain = domain_parser(Path(request.domain).read_text(encoding="utf-8"))
    problem = problem_parser(Path(request.problem).read_text(encoding="utf-8"))

    goal = get_goal_formula(request.formulas, tool)

    compiled_domain, compiled_problem = compile_instance(
        domain, problem, goal, tool
    )

    planning_task = PlanningTask(
        domain=compiled_domain, problem=compiled_problem
    )

    compiled_plans = get_plans(planning_task)

    lemming_task = LemmingTask(
        planning_task=planning_task, plans=compiled_plans
    )

    return lemming_task
