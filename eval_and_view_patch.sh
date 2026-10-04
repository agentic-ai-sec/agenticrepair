python -m secb.evaluator.eval_instances \
    --input-dir ./results/<YOUR_RESULT_FOLDER> \
    --type patch \
    --split cve \
    --agent smolagent \
    --mode all \
    --output-dir ./output/eval/patch

python -m secb.evaluator.view_patch_results \
    --agent smolagent \
    --input-dir ./output/eval/patch