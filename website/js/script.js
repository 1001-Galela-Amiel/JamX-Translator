function displayGithub() {
  window.location.href =
    "https://github.com/JohnnieM-CS426/GameTranslationTool";
}

let inviteButton = document.querySelector("button");
inviteButton.addEventListener("click", displayGithub);

function displayDownloadPage() {
  let download = document.querySelector(".download-page");

  if (download.classList.contains("show")) {
    download.classList.remove("show");
  } else {
    download.classList.add("show");
  }

  document.body.innerHTML = "";
  document.body.appendChild(download);
}
let downloadButton = document.querySelector(".firstButton");
downloadButton.addEventListener("click", displayDownloadPage);

