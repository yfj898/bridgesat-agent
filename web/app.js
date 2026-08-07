const profileForm = document.querySelector("#profile-form");
const diagnosticCard = document.querySelector("#diagnostic-card");
const resultCard = document.querySelector("#result-card");
const questionList = document.querySelector("#question-list");
const networkStatus = document.querySelector("#network-status");

let student = null;
let questions = [];

function updateNetworkStatus() {
  networkStatus.textContent = navigator.onLine
    ? "Online — progress can sync"
    : "Offline — cached lessons remain available";
}

window.addEventListener("online", updateNetworkStatus);
window.addEventListener("offline", updateNetworkStatus);
updateNetworkStatus();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => undefined);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function renderQuestions() {
  questionList.replaceChildren();
  for (const question of questions) {
    const wrapper = document.createElement("div");
    wrapper.className = "question";

    const label = document.createElement("label");
    label.textContent = question.prompt;

    const select = document.createElement("select");
    select.dataset.questionId = question.id;
    select.required = true;
    select.innerHTML = '<option value="">Choose an answer</option>';

    for (const choice of question.choices) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      select.append(option);
    }
    label.append(select);
    wrapper.append(label);
    questionList.append(wrapper);
  }
}

profileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = profileForm.querySelector("button");
  button.disabled = true;
  try {
    student = await request("/v1/students", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#name").value,
        daily_minutes: Number(document.querySelector("#minutes").value),
        target_score: 1200,
      }),
    });
    questions = await request("/v1/questions");
    renderQuestions();
    diagnosticCard.classList.remove("hidden");
    diagnosticCard.scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#submit-diagnostic").addEventListener("click", async () => {
  const selects = [...questionList.querySelectorAll("select")];
  if (selects.some((select) => !select.value)) {
    alert("Please answer every diagnostic question.");
    return;
  }

  try {
    const result = await request("/v1/diagnostics", {
      method: "POST",
      body: JSON.stringify({
        student_id: student.id,
        answers: selects.map((select) => ({
          question_id: select.dataset.questionId,
          selected_answer: select.value,
          hint_level: 0,
        })),
      }),
    });

    document.querySelector("#agent-explanation").textContent = result.agent_explanation;
    const masteryList = document.querySelector("#mastery-list");
    masteryList.replaceChildren();
    for (const [skill, mastery] of Object.entries(result.mastery)) {
      const row = document.createElement("div");
      row.className = "mastery-row";
      row.innerHTML = `<span>${skill.replaceAll("_", " ")}</span><strong>${Math.round(mastery * 100)}%</strong>`;
      masteryList.append(row);
    }

    const planList = document.querySelector("#plan-list");
    planList.replaceChildren();
    for (const item of result.plan) {
      const row = document.createElement("li");
      row.textContent = `${item.minutes} min — ${item.activity.replaceAll("_", " ")} (${item.skill.replaceAll("_", " ")}): ${item.reason}`;
      planList.append(row);
    }
    resultCard.classList.remove("hidden");
    resultCard.scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    alert(error.message);
  }
});
