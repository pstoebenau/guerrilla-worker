use regex::Regex;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    sync::{LazyLock, Mutex},
};
fn expression(pattern: &str) -> Regex {
    static CACHE: LazyLock<Mutex<BTreeMap<String, Regex>>> =
        LazyLock::new(|| Mutex::new(BTreeMap::new()));
    CACHE
        .lock()
        .unwrap()
        .entry(pattern.to_string())
        .or_insert_with(|| Regex::new(pattern).unwrap())
        .clone()
}

pub fn description(stage: &str) -> &str {
    match stage {
        "selection" => "Selecting sharp, well-spaced frames",
        "reconstruction" => "Reconstructing cameras and scene geometry",
        "densification" => "Building the initial dense point cloud",
        "training" => "Loading the dataset and preparing training",
        "export" => "Creating and verifying scene exports",
        _ => "Working",
    }
}
fn number(text: &str) -> Option<f64> {
    text.replace(',', "")
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite())
}
fn counter(capture: &regex::Captures, label: &str, unit: &str) -> Value {
    let current = number(&capture[1]);
    let total = number(&capture[2]);
    if let (Some(current), Some(total)) = (current, total) {
        if current >= 0. && total > 0. && current <= total {
            return json!({"substep":label,"current":current,"total":total,"unit":unit});
        }
    }
    json!({})
}
fn seconds(text: &str) -> Option<f64> {
    let values = expression(r"\d+(?:\.\d+)?")
        .find_iter(text)
        .filter_map(|value| number(value.as_str()))
        .collect::<Vec<_>>();
    if values.is_empty() {
        None
    } else {
        Some(
            values
                .into_iter()
                .rev()
                .enumerate()
                .map(|(index, value)| value * 60_f64.powi(index as i32))
                .sum(),
        )
    }
}

pub fn parse(stage: &str, text: &str) -> Value {
    let text = expression(r"\x1b\[[0-?]*[ -/]*[@-~]").replace_all(text, "");
    let mut result = json!({});
    for line in text.split(['\r', '\n']) {
        let mut update = json!({});
        match stage {
            "training" => {
                if let Some(capture) = expression(
                    r"(?i)\b(?:Training|step|iter(?:ation)?)\b.*?(\d[\d,]*)\s*/\s*(\d[\d,]*)",
                )
                .captures(line)
                {
                    update = counter(&capture, "Optimizing Gaussians", "iterations");
                    if !update.as_object().unwrap().is_empty() {
                        if let Some(eta) = expression(r"(?i)ETA\s+([\d:.]+)|<([\d:hm.s]+)\]")
                            .captures(line)
                            .and_then(|capture| {
                                capture
                                    .get(1)
                                    .or_else(|| capture.get(2))
                                    .and_then(|value| seconds(value.as_str()))
                            })
                        {
                            update["remainingSeconds"] = json!(eta);
                        }
                        for (key, pattern) in [
                            ("gaussians", r"(?i)\b(?:splats|gaussians)\s*[:=]?\s*"),
                            ("loss", r"(?i)\b(?:rgb_loss|loss)\s*[:=]\s*"),
                            ("psnr", r"(?i)\bpsnr\s*[:=]\s*"),
                        ] {
                            let pattern =
                                format!("{pattern}([\\d,]+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)");
                            if let Some(value) = expression(&pattern)
                                .captures(line)
                                .and_then(|capture| number(&capture[1]))
                            {
                                update[key] = json!(value);
                            }
                        }
                    }
                }
                if expression(r"(?i)Training completed|Training complete\.").is_match(line) {
                    update = json!({"substep":"Saving trained scene"});
                }
                if let Some(count) = expression(r"(?i)Final splats:\s*([\d,]+)")
                    .captures(line)
                    .and_then(|capture| number(&capture[1]))
                {
                    update["substep"] = json!("Saving trained scene");
                    update["gaussians"] = json!(count);
                }
            }
            "selection" => {
                for (pattern, label, unit) in [
                    (
                        r"(?i)scanned (\d+)/(\d+) frames",
                        "Scoring video frames",
                        "frames",
                    ),
                    (
                        r"(?i)Exported (\d+)/(\d+) (?:PNGs|JPEGs)",
                        "Exporting selected frames",
                        "images",
                    ),
                ] {
                    if let Some(capture) = expression(pattern).captures(line) {
                        update = counter(&capture, label, unit);
                    }
                }
                if line.contains("Selecting from") {
                    update = json!({"substep":"Choosing distinct, sharp views"});
                }
            }
            "densification" => {
                for (prefix, label, unit) in [
                    ("Preparing views", "Preparing dense reconstruction", "views"),
                    ("Matching image pairs", "Matching image pairs", "pairs"),
                    ("Refining points", "Refining dense points", "points"),
                    ("Fusing points", "Merging dense points", "percent"),
                    ("Removing outliers", "Removing dense outliers", "points"),
                    (
                        "Exporting the point cloud",
                        "Writing dense point cloud",
                        "points",
                    ),
                ] {
                    let pattern = format!(r"^{}: (\d[\d,]*)/(\d[\d,]*)", prefix);
                    if let Some(capture) = expression(&pattern).captures(line.trim()) {
                        update = counter(&capture, label, unit);
                    } else if line.trim().starts_with(prefix) {
                        update = json!({"substep":label});
                        if unit == "percent" {
                            if let Some(capture) = expression(r": (\d+)%$").captures(line.trim()) {
                                if let Some(current) =
                                    number(&capture[1]).filter(|value| *value <= 100.)
                                {
                                    update = json!({"substep":label,"current":current,"total":100,"unit":"percent"});
                                }
                            }
                        }
                    }
                }
                if line.starts_with("Loading the RoMa model") {
                    update = json!({"substep":"Loading the RoMa model"});
                }
            }
            "reconstruction" => {
                for (pattern, label) in [
                    (
                        r"(?i)extracting features|feature extraction|\[extract\]",
                        "Extracting image features",
                    ),
                    (r"(?i)matching|\[match\]", "Matching image features"),
                    (
                        r"(?i)triangulat|registering|\[map\]",
                        "Reconstructing camera poses",
                    ),
                    (
                        r"(?i)bundle adjustment",
                        "Refining cameras and scene geometry",
                    ),
                    (r"(?i)\[orient\]", "Orienting the scene"),
                ] {
                    if expression(pattern).is_match(line) {
                        update = json!({"substep":label});
                    }
                }
                if let Some(capture) = expression(
                    r"(?i)(?:Processed file|Extracted features for image)\s*\[?(\d+)\s*/\s*(\d+)",
                )
                .captures(line)
                {
                    update = counter(&capture, "Extracting image features", "images");
                }
            }
            _ => (),
        }
        if !update.as_object().unwrap().is_empty() {
            if update["substep"] != result["substep"] {
                result = update;
            } else {
                for (key, value) in update.as_object().unwrap() {
                    result[key] = value.clone();
                }
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_training_metrics_and_eta_are_bounded() {
        let value=parse("training","step 12/20 (60%) splats 1,000 [elapsed 00:00:01 | ETA 00:00:02.500] rgb_loss=0.25 psnr=12.4");
        assert_eq!(value["current"], 12.);
        assert_eq!(value["gaussians"], 1000.);
        assert_eq!(value["remainingSeconds"], 2.5);
        assert!(parse("training", "step 25/20 splats 1000")
            .as_object()
            .unwrap()
            .is_empty());
    }
    #[test]
    fn stages_translate_only_known_diagnostics() {
        assert_eq!(parse("selection", "scanned 10/100 frames")["total"], 100.);
        assert_eq!(
            parse("densification", "Matching image pairs: 1/4")["current"],
            1.
        );
        assert_eq!(
            parse("reconstruction", "bundle adjustment")["substep"],
            "Refining cameras and scene geometry"
        );
        assert!(parse("reconstruction", "secret engine debug output")
            .as_object()
            .unwrap()
            .is_empty());
    }
}
