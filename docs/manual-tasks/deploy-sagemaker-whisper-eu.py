#!/usr/bin/env python3
"""
Provision the EU Speech-to-Text endpoint: Whisper large-v3 on Amazon SageMaker
(real-time, eu-central-1/Frankfurt), for the AI-Bridge STT_PROVIDER=aws-sagemaker path.

WHY THIS SCRIPT EXISTS
----------------------
The Bridge code (src/main.py _resolve_stt_provider / _sagemaker_transcribe_sync) is
DONE and unit-tested, but it needs a live SageMaker endpoint to point at. The Bridge's
own AWS key (IAM user `AI-Reporter-Backend`, acct 585768177292) is **Bedrock-scoped** —
verified 2026-07-06: sagemaker:* and iam:GetUser return AccessDenied. So the endpoint
CANNOT be created by the Bridge/agent; it needs an **admin AWS profile**. This script is
that last manual step, reduced to one command.

CONTRACT THE BRIDGE EXPECTS (must match — see _sagemaker_transcribe_sync):
  - Deploy via the HuggingFace ASR inference toolkit (HF DLC), task
    automatic-speech-recognition, model openai/whisper-large-v3.
  - The endpoint accepts RAW AUDIO BYTES with an audio/* content-type (the HF toolkit
    decodes mp3/webm/wav via ffmpeg internally and resamples to 16 kHz) and returns
    JSON {"text": "..."}. That is exactly what the Bridge sends/expects.
  - Do NOT use the JumpStart packaged whisper-large-v3 model — it has a different
    payload contract (requires an explicit `language` param + 30s limit). If you use
    that instead, _sagemaker_transcribe_sync must be adjusted.

PREREQUISITES
  pip install "sagemaker>=2.200" boto3
  # An AWS admin profile with sagemaker:*, iam:CreateRole/AttachRolePolicy (or an
  # existing SageMaker execution role ARN passed via --role-arn).
  export AWS_PROFILE=<admin-profile>      # NOT the Bedrock key — it lacks sagemaker:*

USAGE
  python3 deploy-sagemaker-whisper-eu.py \
      --endpoint-name whisper-large-v3-eu \
      --instance-type ml.g4dn.xlarge \
      --region eu-central-1 \
      [--role-arn arn:aws:iam::585768177292:role/<existing-sagemaker-exec-role>]

COST WARNING (this is the decision only Rafael can make)
  ml.g4dn.xlarge real-time endpoint = a STANDING GPU instance (no scale-to-zero) —
  roughly ~1.4 USD/h in eu-central-1 (~1000 USD/mo) as of 2026; verify current pricing.
  SageMaker Serverless has NO GPU (Whisper not viable). Async inference scales to zero
  but is not synchronous (would break the Bridge endpoint contract). Tear the endpoint
  down with: aws sagemaker delete-endpoint --endpoint-name <name> --region eu-central-1

AFTER THE ENDPOINT IS `InService`
  1. Grant the Bridge's Bedrock key permission to CALL it (least privilege):
       aws iam put-user-policy --user-name AI-Reporter-Backend \
         --policy-name stt-invoke-whisper-eu \
         --policy-document file://iam-invoke-whisper-eu.json   # see runbook for JSON
  2. Set Bridge env (Infisical dev-server -> flows into containers via env_file):
       STT_PROVIDER=aws-sagemaker
       AWS_STT_SAGEMAKER_ENDPOINT=<endpoint-name>
     (Do NOT set STT_PROVIDER=aws-sagemaker BEFORE the endpoint is InService + the IAM
      policy is attached — the Bridge is fail-loud and every dictation would 500.)
  3. Deploy the Bridge: scripts/bridge-deploy.sh both
  4. Live-verify: POST a real audio clip to the Bridge /v1/audio/transcriptions and
     confirm {"text": ...} + the call shows up in SageMaker metrics (eu-central-1).
"""
import argparse
import sys


def main() -> int:
    p = argparse.ArgumentParser(description="Deploy Whisper large-v3 to SageMaker (EU)")
    p.add_argument("--endpoint-name", default="whisper-large-v3-eu")
    p.add_argument("--instance-type", default="ml.g4dn.xlarge")
    p.add_argument("--region", default="eu-central-1")
    p.add_argument("--role-arn", default=None,
                   help="Existing SageMaker execution role ARN; if omitted, "
                        "sagemaker.get_execution_role() is used (needs to run where a "
                        "role is resolvable, e.g. a SageMaker notebook, or pass --role-arn).")
    p.add_argument("--model-id", default="openai/whisper-large-v3")
    # HF DLC version combo — VERIFY against the current list before running:
    # https://github.com/aws/deep-learning-containers/blob/master/available_images.md
    p.add_argument("--transformers-version", default="4.37.0")
    p.add_argument("--pytorch-version", default="2.1.0")
    p.add_argument("--py-version", default="py310")
    args = p.parse_args()

    try:
        import boto3
        import sagemaker
        from sagemaker.huggingface import HuggingFaceModel
    except ImportError as e:
        print(f"Missing dependency: {e}. Run: pip install 'sagemaker>=2.200' boto3", file=sys.stderr)
        return 2

    boto_sess = boto3.Session(region_name=args.region)
    sess = sagemaker.Session(boto_session=boto_sess)

    role = args.role_arn
    if not role:
        try:
            role = sagemaker.get_execution_role(sagemaker_session=sess)
        except Exception as e:
            print("Could not resolve an execution role automatically. Pass --role-arn "
                  f"with a SageMaker execution role that can pull ECR + write CloudWatch.\n{e}",
                  file=sys.stderr)
            return 2

    print(f"Region:        {args.region}")
    print(f"Endpoint:      {args.endpoint_name}")
    print(f"Instance:      {args.instance_type}  (STANDING GPU COST — see header)")
    print(f"Model:         {args.model_id}  (task=automatic-speech-recognition)")
    print(f"Exec role:     {role}")
    print("Deploying (pulls the HF ASR DLC, ~several minutes)…")

    model = HuggingFaceModel(
        role=role,
        transformers_version=args.transformers_version,
        pytorch_version=args.pytorch_version,
        py_version=args.py_version,
        env={
            "HF_MODEL_ID": args.model_id,
            "HF_TASK": "automatic-speech-recognition",
            # Long-form chunking so clips >30s work (Bridge dictation clips are short,
            # but this removes the 30s single-shot limit):
            "CHUNK_LENGTH_S": "30",
        },
        sagemaker_session=sess,
    )

    predictor = model.deploy(
        initial_instance_count=1,
        instance_type=args.instance_type,
        endpoint_name=args.endpoint_name,
    )
    print(f"\n✅ Endpoint '{predictor.endpoint_name}' is InService in {args.region}.")
    print("Next: attach the invoke IAM policy to AI-Reporter-Backend, set the Bridge env, "
          "deploy the Bridge, and live-verify (see this script's header + the runbook).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
