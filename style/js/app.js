document.addEventListener("DOMContentLoaded", () => {
  const dateLabel = document.getElementById("today-label");
  if (dateLabel) dateLabel.textContent = new Intl.DateTimeFormat(undefined, {weekday:"short", month:"short", day:"numeric"}).format(new Date());

  const localDate = (date) => {
    const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return d.toISOString().slice(0, 10);
  };
  const today = localDate(new Date());
  ["due-date-input", "reminder-date-input"].forEach(id => {
    const input = document.getElementById(id);
    if (input) {
      input.min = today;
      if (!input.value) input.value = today;
    }
  });

  document.querySelectorAll(".flash-close").forEach(btn => btn.addEventListener("click", () => btn.closest(".flash").remove()));
  document.querySelectorAll(".filter-pill").forEach(btn => btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-pill").forEach(x => x.classList.remove("selected"));
    btn.classList.add("selected");
    let count = 0;
    document.querySelectorAll(".task-item").forEach(item => {
      const show = btn.dataset.filter === "all" || item.dataset.status === btn.dataset.filter;
      item.hidden = !show;
      if (show) count++;
    });
    const target = document.getElementById("visible-count");
    if (target) target.textContent = count;
  }));
  document.querySelectorAll(".delete-form").forEach(form => form.addEventListener("submit", e => {
    if (!window.confirm(form.dataset.confirm || "Are you sure?")) e.preventDefault();
  }));

  // Normalize typed 24-hour time as HH:MM while preserving keyboard entry.
  document.querySelectorAll('input[name="due_time"], input[name="reminder_time"], input[name="daily_time"]').forEach(input => {
    input.addEventListener("input", () => {
      input.setCustomValidity("");
    });
    input.addEventListener("invalid", () => {
      input.setCustomValidity("Enter time in 24-hour HH:MM format, for example 09:30.");
    });
  });
});
