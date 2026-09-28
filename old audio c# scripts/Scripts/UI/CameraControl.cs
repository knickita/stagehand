using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class CameraControl : MonoBehaviour
{
    UIManager manager;

    public bool activateDragging;
    // Start is called before the first frame update
    void Start()
    {
        manager = FindObjectOfType<UIManager>();        
        activateDragging=false;
    }

    // Update is called once per frame
    void Update()
    {        
        if (Input.GetMouseButton(0) && activateDragging)
        {
            float mouseX = Input.GetAxis("Mouse X");
            float mouseY = Input.GetAxis("Mouse Y");                

            Vector3 panAmount = (transform.up * -mouseY + transform.right * -mouseX) * Camera.main.orthographicSize;
            transform.position += panAmount*.1f;
        }

        if (Input.GetMouseButtonUp(0)){
            activateDragging=false;
        }

        float scroll = Input.GetAxis("Mouse ScrollWheel");
        float zoomSpeed = .8f; // Adjust the zoom speed as needed
        float orthographicSize = Camera.main.orthographicSize;
        orthographicSize *= 1-scroll * zoomSpeed;
        //orthographicSize = Mathf.Clamp(orthographicSize, 1f, 10f); // Adjust the min and max zoom levels as needed
        Camera.main.orthographicSize = orthographicSize;
    }
}
