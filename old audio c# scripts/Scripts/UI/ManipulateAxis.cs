using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class ManipulateAxis : MonoBehaviour
{

    public bool activateDragging;

    public Vector3 axis;

    Vector3 oldMousePos;

    Manipulate manipulate;
    // Start is called before the first frame update
    void Start()
    {
        activateDragging=false;

        manipulate = GetComponentInParent<Manipulate>();        
    }

    // Update is called once per frame
    void Update()
    {   
        if (Input.GetMouseButtonDown(0)){
            oldMousePos= Input.mousePosition;
        }

        if (Input.GetMouseButton(0) && activateDragging){       
            Vector3 direction = Input.mousePosition-oldMousePos;            

            direction=Camera.main.transform.rotation*direction;

            direction*= Camera.main.orthographicSize*0.001f;

            direction=Vector3.Scale(direction, axis);

            manipulate.Move(direction);
            oldMousePos=Input.mousePosition;
        }

        if (Input.GetMouseButtonUp(0)){
            activateDragging=false;
        }
        
    }


}
