(() => {
  const DATA_URL = "/static/data/peru-ubigeo.json";
  let locationsPromise;

  const titleCase = (value) => String(value || "").toLocaleLowerCase("es-PE")
    .replace(/(^|\s|[-'])\p{L}/gu, (letter) => letter.toLocaleUpperCase("es-PE"));

  function getLocations() {
    if (!locationsPromise) {
      locationsPromise = fetch(DATA_URL).then((response) => {
        if (!response.ok) throw new Error("No se pudo cargar la lista de ubicaciones.");
        return response.json();
      });
    }
    return locationsPromise;
  }

  function fill(select, rows, placeholder, getValue, getLabel) {
    select.replaceChildren(new Option(placeholder, ""));
    rows.forEach((row) => select.add(new Option(getLabel(row), getValue(row))));
    select.disabled = rows.length === 0;
  }

  async function bindPeruLocation(form, initial = {}) {
    if (!form) return false;
    const departmentSelect = form.elements.region;
    const provinceSelect = form.elements.city;
    const districtSelect = form.elements.district;
    if (!departmentSelect || !provinceSelect || !districtSelect) return false;

    const departments = await getLocations();
    fill(departmentSelect, departments, "Selecciona un departamento", (row) => row.name, (row) => titleCase(row.name));
    fill(provinceSelect, [], "Selecciona una provincia", (row) => row.name, (row) => titleCase(row.name));
    fill(districtSelect, [], "Selecciona una ciudad o distrito", (row) => row, titleCase);

    const provincesFor = (departmentName) => departments.find((row) => row.name === departmentName)?.provinces || [];
    const districtsFor = (departmentName, provinceName) => provincesFor(departmentName).find((row) => row.name === provinceName)?.districts || [];

    departmentSelect.onchange = () => {
      fill(provinceSelect, provincesFor(departmentSelect.value), "Selecciona una provincia", (row) => row.name, (row) => titleCase(row.name));
      fill(districtSelect, [], "Selecciona una ciudad o distrito", (row) => row, titleCase);
    };
    provinceSelect.onchange = () => {
      fill(districtSelect, districtsFor(departmentSelect.value, provinceSelect.value), "Selecciona una ciudad o distrito", (row) => row, titleCase);
    };

    const normalize = (value) => String(value || "").trim().toLocaleLowerCase("es-PE");
    const savedValues = new Set([initial.region, initial.city, initial.district].map(normalize).filter(Boolean));
    let department = departments.find((row) => normalize(row.name) === normalize(initial.region));
    let province = department && provincesFor(department.name).find((row) => normalize(row.name) === normalize(initial.city));
    let district = province && districtsFor(department.name, province.name).find((name) => normalize(name) === normalize(initial.district));

    // Older saved addresses may have province and district in the wrong columns.
    if (!department || !province || !district) {
      for (const candidateDepartment of departments) {
        const match = candidateDepartment.provinces.find((candidateProvince) => {
          if (!savedValues.has(normalize(candidateProvince.name))) return false;
          return candidateProvince.districts.some((name) => savedValues.has(normalize(name)));
        });
        if (match) {
          department = candidateDepartment;
          province = match;
          district = match.districts.find((name) => savedValues.has(normalize(name)));
          break;
        }
      }
    }

    if (department) {
      departmentSelect.value = department.name;
      departmentSelect.dispatchEvent(new Event("change"));
      if (province) {
        provinceSelect.value = province.name;
        provinceSelect.dispatchEvent(new Event("change"));
        if (district) districtSelect.value = district;
      }
    }
    return true;
  }

  window.bindPeruLocation = bindPeruLocation;
})();
